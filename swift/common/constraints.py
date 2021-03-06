# Copyright (c) 2010-2012 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import urllib
import time
from urllib import unquote
from ConfigParser import ConfigParser, NoSectionError, NoOptionError

from swift.common import utils, exceptions
from swift.common.swob import HTTPBadRequest, HTTPLengthRequired, \
    HTTPRequestEntityTooLarge, HTTPPreconditionFailed, HTTPNotImplemented, \
    HTTPException

MAX_FILE_SIZE = 5368709122
MAX_META_NAME_LENGTH = 128
MAX_META_VALUE_LENGTH = 256
MAX_META_COUNT = 90
MAX_META_OVERALL_SIZE = 4096
MAX_HEADER_SIZE = 8192
MAX_OBJECT_NAME_LENGTH = 1024
CONTAINER_LISTING_LIMIT = 10000
ACCOUNT_LISTING_LIMIT = 10000
MAX_ACCOUNT_NAME_LENGTH = 256
MAX_CONTAINER_NAME_LENGTH = 256

# If adding an entry to DEFAULT_CONSTRAINTS, note that
# these constraints are automatically published by the
# proxy server in responses to /info requests, with values
# updated by reload_constraints()
DEFAULT_CONSTRAINTS = {
    'max_file_size': MAX_FILE_SIZE,
    'max_meta_name_length': MAX_META_NAME_LENGTH,
    'max_meta_value_length': MAX_META_VALUE_LENGTH,
    'max_meta_count': MAX_META_COUNT,
    'max_meta_overall_size': MAX_META_OVERALL_SIZE,
    'max_header_size': MAX_HEADER_SIZE,
    'max_object_name_length': MAX_OBJECT_NAME_LENGTH,
    'container_listing_limit': CONTAINER_LISTING_LIMIT,
    'account_listing_limit': ACCOUNT_LISTING_LIMIT,
    'max_account_name_length': MAX_ACCOUNT_NAME_LENGTH,
    'max_container_name_length': MAX_CONTAINER_NAME_LENGTH,
}

SWIFT_CONSTRAINTS_LOADED = False
OVERRIDE_CONSTRAINTS = {}  # any constraints overridden by SWIFT_CONF_FILE
EFFECTIVE_CONSTRAINTS = {}  # populated by reload_constraints


def reload_constraints():
    """
    Parse SWIFT_CONF_FILE and reset module level global contraint attrs,
    populating OVERRIDE_CONSTRAINTS AND EFFECTIVE_CONSTRAINTS along the way.
    """
    global SWIFT_CONSTRAINTS_LOADED, OVERRIDE_CONSTRAINTS
    SWIFT_CONSTRAINTS_LOADED = False
    OVERRIDE_CONSTRAINTS = {}
    constraints_conf = ConfigParser()
    if constraints_conf.read(utils.SWIFT_CONF_FILE):
        SWIFT_CONSTRAINTS_LOADED = True
        for name in DEFAULT_CONSTRAINTS:
            try:
                value = int(constraints_conf.get('swift-constraints', name))
            except NoOptionError:
                pass
            except NoSectionError:
                # We are never going to find the section for another option
                break
            else:
                OVERRIDE_CONSTRAINTS[name] = value
    for name, default in DEFAULT_CONSTRAINTS.items():
        value = OVERRIDE_CONSTRAINTS.get(name, default)
        EFFECTIVE_CONSTRAINTS[name] = value
        # "globals" in this context is module level globals, always.
        globals()[name.upper()] = value


reload_constraints()


# Maximum slo segments in buffer
MAX_BUFFERED_SLO_SEGMENTS = 10000


#: Query string format= values to their corresponding content-type values
FORMAT2CONTENT_TYPE = {'plain': 'text/plain', 'json': 'application/json',
                       'xml': 'application/xml'}


def check_metadata(req, target_type):
    """
    Check metadata sent in the request headers.

    :param req: request object
    :param target_type: str: one of: object, container, or account: indicates
                        which type the target storage for the metadata is
    :returns: HTTPBadRequest with bad metadata otherwise None
    """
    prefix = 'x-%s-meta-' % target_type.lower()
    meta_count = 0
    meta_size = 0
    for key, value in req.headers.iteritems():
        if isinstance(value, basestring) and len(value) > MAX_HEADER_SIZE:
            return HTTPBadRequest(body='Header value too long: %s' %
                                  key[:MAX_META_NAME_LENGTH],
                                  request=req, content_type='text/plain')
        if not key.lower().startswith(prefix):
            continue
        key = key[len(prefix):]
        if not key:
            return HTTPBadRequest(body='Metadata name cannot be empty',
                                  request=req, content_type='text/plain')
        meta_count += 1
        meta_size += len(key) + len(value)
        if len(key) > MAX_META_NAME_LENGTH:
            return HTTPBadRequest(
                body='Metadata name too long: %s%s' % (prefix, key),
                request=req, content_type='text/plain')
        elif len(value) > MAX_META_VALUE_LENGTH:
            return HTTPBadRequest(
                body='Metadata value longer than %d: %s%s' % (
                    MAX_META_VALUE_LENGTH, prefix, key),
                request=req, content_type='text/plain')
        elif meta_count > MAX_META_COUNT:
            return HTTPBadRequest(
                body='Too many metadata items; max %d' % MAX_META_COUNT,
                request=req, content_type='text/plain')
        elif meta_size > MAX_META_OVERALL_SIZE:
            return HTTPBadRequest(
                body='Total metadata too large; max %d'
                % MAX_META_OVERALL_SIZE,
                request=req, content_type='text/plain')
    return None


def check_object_creation(req, object_name):
    """
    Check to ensure that everything is alright about an object to be created.

    :param req: HTTP request object
    :param object_name: name of object to be created
    :returns HTTPRequestEntityTooLarge: the object is too large
    :returns HTTPLengthRequired: missing content-length header and not
                                 a chunked request
    :returns HTTPBadRequest: missing or bad content-type header, or
                             bad metadata
    :returns HTTPNotImplemented: unsupported transfer-encoding header value
    """
    try:
        ml = req.message_length()
    except ValueError as e:
        return HTTPBadRequest(request=req, content_type='text/plain',
                              body=str(e))
    except AttributeError as e:
        return HTTPNotImplemented(request=req, content_type='text/plain',
                                  body=str(e))
    if ml is not None and ml > MAX_FILE_SIZE:
        return HTTPRequestEntityTooLarge(body='Your request is too large.',
                                         request=req,
                                         content_type='text/plain')
    if req.content_length is None and \
            req.headers.get('transfer-encoding') != 'chunked':
        return HTTPLengthRequired(body='Missing Content-Length header.',
                                  request=req,
                                  content_type='text/plain')

    if 'X-Copy-From' in req.headers and req.content_length:
        return HTTPBadRequest(body='Copy requests require a zero byte body',
                              request=req, content_type='text/plain')

    if len(object_name) > MAX_OBJECT_NAME_LENGTH:
        return HTTPBadRequest(body='Object name length of %d longer than %d' %
                              (len(object_name), MAX_OBJECT_NAME_LENGTH),
                              request=req, content_type='text/plain')

    if 'Content-Type' not in req.headers:
        return HTTPBadRequest(request=req, content_type='text/plain',
                              body='No content type')

    try:
        req = check_delete_headers(req)
    except HTTPException as e:
        return HTTPBadRequest(request=req, body=e.body,
                              content_type='text/plain')

    if not check_utf8(req.headers['Content-Type']):
        return HTTPBadRequest(request=req, body='Invalid Content-Type',
                              content_type='text/plain')
    return check_metadata(req, 'object')


def check_mount(root, drive):
    """
    Verify that the path to the device is a mount point and mounted.  This
    allows us to fast fail on drives that have been unmounted because of
    issues, and also prevents us for accidentally filling up the root
    partition.

    :param root:  base path where the devices are mounted
    :param drive: drive name to be checked
    :returns: True if it is a valid mounted device, False otherwise
    """
    if not (urllib.quote_plus(drive) == drive):
        return False
    path = os.path.join(root, drive)
    return utils.ismount(path)


def check_float(string):
    """
    Helper function for checking if a string can be converted to a float.

    :param string: string to be verified as a float
    :returns: True if the string can be converted to a float, False otherwise
    """
    try:
        float(string)
        return True
    except ValueError:
        return False


def valid_timestamp(request):
    """
    Helper function to extract a timestamp from requests that require one.

    :param request: the swob request object

    :returns: a valid Timestamp instance
    :raises: HTTPBadRequest on missing or invalid X-Timestamp
    """
    try:
        return request.timestamp
    except exceptions.InvalidTimestamp as e:
        raise HTTPBadRequest(body=str(e), request=request,
                             content_type='text/plain')


def check_delete_headers(request):
    """
    Validate if 'x-delete' headers are have correct values
    values should be positive integers and correspond to
    a time in the future.

    :param request: the swob request object

    :returns: HTTPBadRequest in case of invalid values
              or None if values are ok
    """
    if 'x-delete-after' in request.headers:
        try:
            x_delete_after = int(request.headers['x-delete-after'])
        except ValueError:
            raise HTTPBadRequest(request=request,
                                 content_type='text/plain',
                                 body='Non-integer X-Delete-After')
        actual_del_time = time.time() + x_delete_after
        if actual_del_time < time.time():
            raise HTTPBadRequest(request=request,
                                 content_type='text/plain',
                                 body='X-Delete-After in past')
        request.headers['x-delete-at'] = utils.normalize_delete_at_timestamp(
            actual_del_time)

    if 'x-delete-at' in request.headers:
        try:
            x_delete_at = int(utils.normalize_delete_at_timestamp(
                int(request.headers['x-delete-at'])))
        except ValueError:
            raise HTTPBadRequest(request=request, content_type='text/plain',
                                 body='Non-integer X-Delete-At')

        if x_delete_at < time.time():
            raise HTTPBadRequest(request=request, content_type='text/plain',
                                 body='X-Delete-At in past')
    return request


def check_utf8(string):
    """
    Validate if a string is valid UTF-8 str or unicode and that it
    does not contain any null character.

    :param string: string to be validated
    :returns: True if the string is valid utf-8 str or unicode and
              contains no null characters, False otherwise
    """
    if not string:
        return False
    try:
        if isinstance(string, unicode):
            string.encode('utf-8')
        else:
            string.decode('UTF-8')
        return '\x00' not in string
    # If string is unicode, decode() will raise UnicodeEncodeError
    # So, we should catch both UnicodeDecodeError & UnicodeEncodeError
    except UnicodeError:
        return False


def check_path_header(req, name, length, error_msg):
    """
    Validate that the value of path-like header is
    well formatted. We assume the caller ensures that
    specific header is present in req.headers.

    :param req: HTTP request object
    :param name: header name
    :param length: length of path segment check
    :param error_msg: error message for client
    :returns: A tuple with path parts according to length
    :raise: HTTPPreconditionFailed if header value
            is not well formatted.
    """
    src_header = unquote(req.headers.get(name))
    if not src_header.startswith('/'):
        src_header = '/' + src_header
    try:
        return utils.split_path(src_header, length, length, True)
    except ValueError:
        raise HTTPPreconditionFailed(
            request=req,
            body=error_msg)


def check_copy_from_header(req):
    """
    Validate that the value from x-copy-from header is
    well formatted. We assume the caller ensures that
    x-copy-from header is present in req.headers.

    :param req: HTTP request object
    :returns: A tuple with container name and object name
    :raise: HTTPPreconditionFailed if x-copy-from value
            is not well formatted.
    """
    return check_path_header(req, 'X-Copy-From', 2,
                             'X-Copy-From header must be of the form '
                             '<container name>/<object name>')


def check_destination_header(req):
    """
    Validate that the value from destination header is
    well formatted. We assume the caller ensures that
    destination header is present in req.headers.

    :param req: HTTP request object
    :returns: A tuple with container name and object name
    :raise: HTTPPreconditionFailed if destination value
            is not well formatted.
    """
    return check_path_header(req, 'Destination', 2,
                             'Destination header must be of the form '
                             '<container name>/<object name>')


def check_account_format(req, account):
    """
    Validate that the header contains valid account name.
    We assume the caller ensures that
    destination header is present in req.headers.

    :param req: HTTP request object
    :returns: A properly encoded account name
    :raise: HTTPPreconditionFailed if account header
            is not well formatted.
    """
    if isinstance(account, unicode):
        account = account.encode('utf-8')
    if '/' in account:
        raise HTTPPreconditionFailed(
            request=req,
            body='Account name cannot contain slashes')
    return account
