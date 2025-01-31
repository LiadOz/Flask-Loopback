import socket
from contextlib import contextmanager

import requests
import six
from requests.cookies import MockRequest
from urllib3 import HTTPResponse

from . import dispatch
from ._compat import httplib, iteritems, gzip_decompress

try:
    from contextlib import ExitStack
except ImportError:
    from contextlib2 import ExitStack


class CustomHTTPResponse(Exception):
    def __init__(self, request, code):
        super(CustomHTTPResponse, self).__init__()
        self.response = requests.Response()
        self.response.url = str(request.url)
        self.response.status_code = code
        self.response.reason = httplib.responses.get(code, None)
        self.response.request = request


class _IOReader(six.BytesIO):
    """A reader that makes a BytesIO look like a HTTPResponse.
    A HTTPResponse will return an empty string when you read from it after
    the socket has been closed. A BytesIO will raise a ValueError. For
    compatibility we want to do the same thing a HTTPResponse does.
    """

    def read(self, *args, **kwargs):  # pylint: disable=arguments-differ
        if self.closed:
            return six.b('')

        # not a new style object in python 2
        result = six.BytesIO.read(self, *args, **kwargs)

        # when using resp.iter_content(None) it'll go through a different
        # request path in urllib3. This path checks whether the object is
        # marked closed instead of the return value. see gh124.
        if result == six.b(''):
            self.close()

        return result


class FlaskLoopback(object):

    def __init__(self, flask_app):
        super(FlaskLoopback, self).__init__()
        self.flask_app = flask_app
        self._test_client = flask_app.test_client(use_cookies=False)
        self._request_context_handlers = []
        self._registered_addresses = set()
        self._use_ssl = {}

    def register_request_context_handler(self, handler):
        self._request_context_handlers.append(handler)

    @contextmanager
    def on(self, address, ssl=False):
        self.activate_address(address, ssl)
        try:
            yield self
        finally:
            self.deactivate_address(address)

    def activate_address(self, address, ssl=False):
        assert isinstance(address, tuple) and len(address) == 2, 'Address must be a tuple of the form (host, port)'
        dispatch.register_loopback_handler(address, self, ssl)
        self._registered_addresses.add(address)

    def deactivate_address(self, address):
        assert isinstance(address, tuple) and len(address) == 2, 'Address must be a tuple of the form (host, port)'
        dispatch.unregister_loopback_handler(address)
        self._registered_addresses.remove(address)

    def deactivate_all(self):
        while self._registered_addresses:
            self.deactivate_address(next(iter(self._registered_addresses)))

    def handle_request(self, session, url, request):
        assert url.scheme
        path = "/{0}".format(url.split("/", 3)[-1])
        request_data = request.body
        if hasattr(request_data, 'read'):
            request_data = request_data.read()
        open_kwargs = {
            'method': request.method.upper(), 'headers': iteritems(request.headers), 'data': request_data,
            'environ_base': {'REMOTE_ADDR': _get_hostname()},
            'base_url': '{0.scheme}://{0.netloc}'.format(url),
        }
        with ExitStack() as stack:
            for handler in self._request_context_handlers:
                try:
                    stack.enter_context(handler(request))  # pylint: disable=no-member
                except CustomHTTPResponse as e:
                    return e.response

            resp = self._test_client.open(path, **open_kwargs)
            returned = requests.Response()
            assert returned.url is None
            returned.url = str(url)
            returned.status_code = resp.status_code
            returned.reason = httplib.responses.get(resp.status_code, None)
            returned.request = request
            resp_data = resp.get_data()
            if resp.headers.get('content-encoding') == 'gzip':
                resp_data = gzip_decompress(resp_data)
            returned._content = resp_data  # pylint: disable=protected-access
            returned.headers.update(resp.headers.items())
            returned.raw = HTTPResponse(
                status=returned.status_code,
                reason=returned.reason,
                headers=returned.headers,
                body=_IOReader(resp_data) or _IOReader(six.b('')),
                decode_content=False,
                preload_content=False,
            )
            self._extract_cookies(session, request, resp, returned)
            return returned

    def _extract_cookies(self, session, request, raw_response, response):
        mocked_response = _MockResponse(raw_response)
        mocked_request = MockRequest(request)
        for obj in (response, session):
            obj.cookies.extract_cookies(mocked_response, mocked_request)


class _MockResponse(object):

    def __init__(self, flask_client_response):
        super(_MockResponse, self).__init__()
        self._resp = flask_client_response

    def info(self):
        return self

    def getheaders(self, name):
        returned = self._resp.headers.get(name.lower())
        if returned is not None:
            return [returned]
        return []

    def get_all(self, name, default=None):
        return self.getheaders(name) or default


_hostname = None


def _get_hostname():
    global _hostname  # pylint: disable=global-statement
    if _hostname is None:
        _hostname = socket.getfqdn()
    return _hostname
