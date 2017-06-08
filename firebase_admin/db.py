# Copyright 2017 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Firebase Realtime Database module.

This module contains functions and classes that facilitate interacting with the Firebase Realtime
Database. It supports basic data manipulation operations, as well as complex queries such as
limit queries and range queries. However, it does not support realtime update notifications. This
module uses the Firebase REST API underneath.
"""

import collections
import json
import numbers

import requests
import six
from six.moves import urllib

from firebase_admin import utils

_DB_ATTRIBUTE = '_database'
_INVALID_PATH_CHARACTERS = '[].#$'
_RESERVED_FILTERS = ('$key', '$value', '$priority')


def get_reference(path='/', app=None):
    """Returns a database Reference representing the node at the specified path.

    If no path is specified, this function returns a Reference that represents the database root.

    Args:
      path: Path to a node in the Firebase realtime database (optional).
      app: An App instance (optional).

    Returns:
      Reference: A newly initialized Reference.

    Raises:
      ValueError: If the specified path or app is invalid.
    """
    client = utils.get_app_service(app, _DB_ATTRIBUTE, _Client.from_app)
    return Reference(client=client, path=path)

def _parse_path(path):
    """Parses a path string into a set of segments."""
    if not isinstance(path, six.string_types):
        raise ValueError('Invalid path: "{0}". Path must be a string.'.format(path))
    if any(ch in path for ch in _INVALID_PATH_CHARACTERS):
        raise ValueError(
            'Invalid path: "{0}". Path contains illegal characters.'.format(path))
    return [seg for seg in path.split('/') if seg]


class Reference(object):
    """Reference represents a node in the Firebase realtime database."""

    def __init__(self, **kwargs):
        """Creates a new Reference using the provided parameters.

        This method is for internal use only. Use db.get_reference() to obtain an instance of
        Reference.
        """
        self._client = kwargs.get('client')
        if 'segments' in kwargs:
            self._segments = kwargs.get('segments')
        else:
            self._segments = _parse_path(kwargs.get('path'))
        self._pathurl = '/' + '/'.join(self._segments)

    @property
    def key(self):
        if self._segments:
            return self._segments[-1]
        return None

    @property
    def path(self):
        return self._pathurl

    @property
    def parent(self):
        if self._segments:
            return Reference(client=self._client, segments=self._segments[:-1])
        return None

    def child(self, path):
        """Returns a Reference to the specified child node.

        The path may point to an immediate child of the current Reference, or a deeply nested
        child. Child paths must not begin with '/'.

        Args:
          path: Path to the child node.

        Returns:
          Reference: A database Reference representing the specified child node.

        Raises:
          ValueError: If the child path is not a string, not well-formed or begins with '/'.
        """
        if not path or not isinstance(path, six.string_types):
            raise ValueError(
                'Invalid path argument: "{0}". Path must be a non-empty string.'.format(path))
        if path.startswith('/'):
            raise ValueError(
                'Invalid path argument: "{0}". Child path must not start with "/"'.format(path))
        full_path = self._pathurl + '/' + path
        return Reference(client=self._client, path=full_path)

    def get_value(self):
        """Returns the value at the current location of the database.

        Returns:
          object: Decoded JSON value of the current database Reference.

        Raises:
          ApiCallError: If an error occurs while communicating with the remote database server.
        """
        return self._client.request('get', self._add_suffix())

    def get_priority(self):
        """Returns the priority of this node, if specified.

        Returns:
          object: A priority value or None.

        Raises:
          ApiCallError: If an error occurs while communicating with the remote database server.
        """
        return self._client.request('get', self._add_suffix('/.priority.json'))

    def set_value(self, value, priority=None):
        """Sets the data at this location to the given value.

        The value must be JSON-serializable and not None. If a priority is specified, the node will
        be assigned that priority along with the value.

        Args:
          value: JSON-serialable value to be set at this location.
          priority: A numeric or alphanumeric priority value (optional).

        Raises:
          ValueError: If the value is None or priority is invalid.
          TypeError: If the value is not JSON-serializable.
          ApiCallError: If an error occurs while communicating with the remote database server.
        """
        if value is None:
            raise ValueError('Value must not be None.')
        if priority is not None:
            Reference._check_priority(priority)
            if isinstance(value, dict):
                value = dict(value)
                value['.priority'] = priority
            else:
                value = {'.value' : value, '.priority' : priority}
        params = {'print' : 'silent'}
        self._client.request_oneway('put', self._add_suffix(), json=value, params=params)

    def push(self, value=''):
        """Creates a new child node.

        The optional value argument can be used to provide an initial value for the child node. If
        no value is provided, child node will have empty string as the default value.

        Args:
          value: JSON-serializable initial value for the child node (optional).

        Returns:
          Reference: A Reference representing the newly created child node.

        Raises:
          ValueError: If the value is None.
          TypeError: If the value is not JSON-serializable.
          ApiCallError: If an error occurs while communicating with the remote database server.
        """
        if value is None:
            raise ValueError('Value must not be None.')
        output = self._client.request('post', self._add_suffix(), json=value)
        push_id = output.get('name')
        return self.child(push_id)

    def update_children(self, value):
        """Updates the specified child keys of this Reference to the provided values.

        Args:
          value: A dictionary containing the child keys to update, and their new values.

        Raises:
          ValueError: If value is empty or not a dictionary.
          ApiCallError: If an error occurs while communicating with the remote database server.
        """
        if not value or not isinstance(value, dict):
            raise ValueError('Value argument must be a non-empty dictionary.')
        if None in value.keys() or None in value.values():
            raise ValueError('Dictionary must not contain None keys or values.')
        params = {'print':'silent'}
        self._client.request_oneway('patch', self._add_suffix(), json=value, params=params)

    def delete(self):
        """Deleted this node from the database.

        Raises:
          ApiCallError: If an error occurs while communicating with the remote database server.
        """
        self._client.request_oneway('delete', self._add_suffix())

    def order_by_child(self, path):
        """Returns a Query that orders data by child values.

        Returned Query can be used to set additional parameters, and execute complex database
        queries (e.g. limit queries, range queries).

        Args:
          path: Path to a valid child of the current Reference.

        Returns:
          Query: A database Query instance.

        Raises:
          ValueError: If the child path is not a string, not well-formed or None.
        """
        if path in _RESERVED_FILTERS:
            raise ValueError('Illegal child path: {0}'.format(path))
        return Query(order_by=path, client=self._client, pathurl=self._add_suffix())

    def order_by_key(self):
        """Creates a Query that orderes data by key.

        Returned Query can be used to set additional parameters, and execute complex database
        queries (e.g. limit queries, range queries).

        Returns:
          Query: A database Query instance.
        """
        return Query(order_by='$key', client=self._client, pathurl=self._add_suffix())

    def order_by_value(self):
        """Creates a Query that orderes data by value.

        Returned Query can be used to set additional parameters, and execute complex database
        queries (e.g. limit queries, range queries).

        Returns:
          Query: A database Query instance.
        """
        return Query(order_by='$value', client=self._client, pathurl=self._add_suffix())

    def order_by_priority(self):
        """Creates a Query that orderes data by priority.

        Returned Query can be used to set additional parameters, and execute complex database
        queries (e.g. limit queries, range queries). Due to a limitation of the
        underlying REST API, the order-by-priority constraint can only be enforced during
        the execution time of the Query. When the Query returns results, the actual results
        will be returned as an unordered collection.

        Returns:
          Query: A database Query instance.
        """
        return Query(order_by='$priority', client=self._client, pathurl=self._add_suffix())

    def _add_suffix(self, suffix='.json'):
        return self._pathurl + suffix

    @classmethod
    def _check_priority(cls, priority):
        if isinstance(priority, six.string_types) and priority.isalnum():
            return
        if isinstance(priority, numbers.Number):
            return
        raise ValueError('Illegal priority value: "{0}". Priority values must be numeric or '
                         'alphanumeric.'.format(priority))


class Query(object):
    """Represents a complex query that can be executed on a Reference.

    Complex queries can consist of up to 2 components: a required ordering constraint, and an
    optional filtering constraint. At the server, data is first sorted according to the given
    ordering constraint (e.g. order by child). Then the filtering constraint (e.g. limit, range)
    is applied on the sorted data to produce the final result. Despite the ordering constraint,
    the final result is returned by the server as an unordered collection. Therefore the Query
    interface performs another round of sorting at the client-side before returning the results
    to the caller. This client-side sorted results are returned to the user as a Python
    OrderedDict. However, client-side sorting is not feasible for order-by-priority queries.
    Therefore for such queries results are returned as a regular unordered dict.
    """

    def __init__(self, **kwargs):
        order_by = kwargs.pop('order_by')
        if not order_by or not isinstance(order_by, six.string_types):
            raise ValueError('order_by field must be a non-empty string')
        if order_by not in _RESERVED_FILTERS:
            if order_by.startswith('/'):
                raise ValueError('Invalid path argument: "{0}". Child path must not start '
                                 'with "/"'.format(order_by))
            segments = _parse_path(order_by)
            order_by = '/'.join(segments)
        self._client = kwargs.pop('client')
        self._pathurl = kwargs.pop('pathurl')
        self._order_by = order_by
        self._params = {'orderBy' : json.dumps(order_by)}
        if kwargs:
            raise ValueError('Unexpected keyword arguments: {0}'.format(kwargs))

    def set_limit_first(self, limit):
        """Creates a query with limit, and anchors it to the start of the window.

        Args:
          limit: The maximum number of child nodes to return.

        Returns:
          Query: The updated Query instance.

        Raises:
          ValueError: If the value is not an integer, or set_limit_last() was called previously.
        """
        if not isinstance(limit, int) or limit < 0:
            raise ValueError('Limit must be a non-negative integer.')
        if 'limitToLast' in self._params:
            raise ValueError('Cannot set both first and last limits.')
        self._params['limitToFirst'] = limit
        return self

    def set_limit_last(self, limit):
        """Creates a query with limit, and anchors it to the end of the window.

        Args:
          limit: The maximum number of child nodes to return.

        Returns:
          Query: The updated Query instance.

        Raises:
          ValueError: If the value is not an integer, or set_limit_first() was called previously.
        """
        if not isinstance(limit, int) or limit < 0:
            raise ValueError('Limit must be a non-negative integer.')
        if 'limitToFirst' in self._params:
            raise ValueError('Cannot set both first and last limits.')
        self._params['limitToLast'] = limit
        return self

    def set_start_at(self, start):
        """Sets the lower bound for a range query.

        The Query will only return child nodes with a value greater than or equal to the specified
        value.

        Args:
          start: JSON-serializable value to start at, inclusive.

        Returns:
          Query: The updated Query instance.

        Raises:
          ValueError: If the value is empty or None.
        """
        if not start:
            raise ValueError('Start value must not be empty or None.')
        self._params['startAt'] = json.dumps(start)
        return self

    def set_end_at(self, end):
        """Sets the upper bound for a range query.

        The Query will only return child nodes with a value less than or equal to the specified
        value.

        Args:
          end: JSON-serializable value to end at, inclusive.

        Returns:
          Query: The updated Query instance.

        Raises:
          ValueError: If the value is empty or None.
        """
        if not end:
            raise ValueError('End value must not be empty or None.')
        self._params['endAt'] = json.dumps(end)
        return self

    def set_equal_to(self, value):
        """Sets an equals constraint on the Query.

        The Query will only return child nodes whose value is equal to the specified value.

        Args:
          value: JSON-serializable value to query for.

        Returns:
          Query: The updated Query instance.

        Raises:
          ValueError: If the value is empty or None.
        """
        if not value:
            raise ValueError('Equal to value must not be empty or None.')
        self._params['equalTo'] = json.dumps(value)
        return self

    @property
    def querystr(self):
        params = []
        for key in sorted(self._params):
            params.append('{0}={1}'.format(key, self._params[key]))
        return '&'.join(params)

    def run(self):
        """Executes this Query and returns the results.

        The results will be returned as a sorted list or an OrderedDict, except in the case of
        order-by-priority queries.

        Returns:
          object: Decoded JSON result of the Query.

        Raises:
          ApiCallError: If an error occurs while communicating with the remote database server.
        """
        result = self._client.request('get', '{0}?{1}'.format(self._pathurl, self.querystr))
        if isinstance(result, (dict, list)) and self._order_by != '$priority':
            return _Sorter(result, self._order_by).get()
        return result


class ApiCallError(Exception):
    """Represents an Exception encountered while invoking the Firebase database server API."""

    def __init__(self, message, error):
        Exception.__init__(self, message)
        self.detail = error


class _Sorter(object):
    """Helper class for sorting query results."""

    def __init__(self, results, order_by):
        if isinstance(results, dict):
            self.dict_input = True
            entries = [_SortEntry(k, v, order_by) for k, v in results.items()]
        elif isinstance(results, list):
            self.dict_input = False
            entries = [_SortEntry(k, v, order_by) for k, v in enumerate(results)]
        else:
            raise ValueError('Sorting not supported for "{0}" object.'.format(type(results)))
        self.sort_entries = sorted(entries)

    def get(self):
        if self.dict_input:
            return collections.OrderedDict([(e.key, e.value) for e in self.sort_entries])
        else:
            return [e.value for e in self.sort_entries]


class _SortEntry(object):
    """A wrapper that is capable of sorting items in a dictionary."""

    _type_none = 0
    _type_bool_false = 1
    _type_bool_true = 2
    _type_numeric = 3
    _type_string = 4
    _type_object = 5

    def __init__(self, key, value, order_by):
        self._key = key
        self._value = value
        if order_by == '$key' or order_by == '$priority':
            self._index = key
        elif order_by == '$value':
            self._index = value
        else:
            self._index = _SortEntry._extract_child(value, order_by)
        self._index_type = _SortEntry._get_index_type(self._index)

    @property
    def key(self):
        return self._key

    @property
    def index(self):
        return self._index

    @property
    def index_type(self):
        return self._index_type

    @property
    def value(self):
        return self._value

    @classmethod
    def _get_index_type(cls, index):
        """Assigns an integer code to the type of the index.

        The index type determines how differently typed values are sorted. This ordering is based
        on https://firebase.google.com/docs/database/rest/retrieve-data#section-rest-ordered-data
        """
        if index is None:
            return cls._type_none
        elif isinstance(index, bool) and not index:
            return cls._type_bool_false
        elif isinstance(index, bool) and index:
            return cls._type_bool_true
        elif isinstance(index, (int, float)):
            return cls._type_numeric
        elif isinstance(index, six.string_types):
            return cls._type_string
        else:
            return cls._type_object

    @classmethod
    def _extract_child(cls, value, path):
        segments = path.split('/')
        current = value
        for segment in segments:
            if isinstance(current, dict):
                current = current.get(segment)
            else:
                return None
        return current

    def _compare(self, other):
        """Compares two _SortEntry instances.

        If the indices have the same numeric or string type, compare them directly. Ties are
        broken by comparing the keys. If the indices have the same type, but are neither numeric
        nor string, compare the keys. In all other cases compare based on the ordering provided
        by index types.
        """
        self_key, other_key = self.index_type, other.index_type
        if self_key == other_key:
            if self_key in (self._type_numeric, self._type_string) and self.index != other.index:
                self_key, other_key = self.index, other.index
            else:
                self_key, other_key = self.key, other.key

        if self_key < other_key:
            return -1
        elif self_key > other_key:
            return 1
        else:
            return 0

    def __lt__(self, other):
        return self._compare(other) < 0

    def __le__(self, other):
        return self._compare(other) <= 0

    def __gt__(self, other):
        return self._compare(other) > 0

    def __ge__(self, other):
        return self._compare(other) >= 0

    def __eq__(self, other):
        return self._compare(other) is 0


class _Client(object):
    """HTTP client used to make REST calls.

    _Client maintains an HTTP session, and handles authenticating HTTP requests along with
    marshalling and unmarshalling of JSON data.
    """

    def __init__(self, url=None, auth=None, session=None):
        self._url = url
        self._auth = auth
        self._session = session

    @classmethod
    def from_app(cls, app):
        """Created a new _Client for a given App"""
        url = app.options.get('dbURL')
        if not url or not isinstance(url, six.string_types):
            raise ValueError(
                'Invalid dbURL option: "{0}". dbURL must be a non-empty URL string.'.format(url))
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != 'https':
            raise ValueError(
                'Invalid dbURL option: "{0}". dbURL must be an HTTPS URL.'.format(url))
        elif not parsed.netloc.endswith('.firebaseio.com'):
            raise ValueError(
                'Invalid dbURL option: "{0}". dbURL must be a valid URL to a Firebase Realtime '
                'Database instance.'.format(url))
        return _Client('https://{0}'.format(parsed.netloc), _OAuth(app), requests.Session())

    def request(self, method, urlpath, **kwargs):
        return self._do_request(method, urlpath, **kwargs).json()

    def request_oneway(self, method, urlpath, **kwargs):
        self._do_request(method, urlpath, **kwargs)

    def _do_request(self, method, urlpath, **kwargs):
        """Makes an HTTP call using the Python requests library.

        Refer to http://docs.python-requests.org/en/master/api/ for more information on supported
        options and features.

        Args:
          method: HTTP method name as a string (e.g. get, post).
          urlpath: URL path of the remote endpoint. This will be appended to the server's base URL.
          kwargs: An additional set of keyword arguments to be passed into requests API
              (e.g. json, params).

        Returns:
          Response: An HTTP response object.

        Raises:
          ApiCallError: If an error occurs while making the HTTP call.
        """
        try:
            resp = self._session.request(method, self._url + urlpath, auth=self._auth, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException as error:
            raise ApiCallError(self._extract_error_message(error), error)

    def _extract_error_message(self, error):
        """Extracts an error message from an exception.

        If the server has not sent any response, simply converts the exception into a string.
        If the server has sent a JSON response with an 'error' field, which is the typical
        behavior of the Realtime Database REST API, parses the response to retrieve the error
        message. If the server has sent a non-JSON response, returns the full response
        as the error message.

        Args:
          error: An exception raised by the requests library.

        Returns:
          str: A string error message extracted from the exception.
        """
        if error.response is None:
            return str(error)
        try:
            data = error.response.json()
            if isinstance(data, dict):
                return '{0}\nReason: {1}'.format(error, data.get('error', 'unknown'))
        except ValueError:
            pass
        return '{0}\nReason: {1}'.format(error, error.response.content.decode())

    def close(self):
        self._session.close()
        self._auth = None
        self._url = None


class _OAuth(requests.auth.AuthBase):
    def __init__(self, app):
        self._app = app

    def __call__(self, req):
        req.headers['Authorization'] = 'Bearer {0}'.format(self._app.get_token())
        return req
