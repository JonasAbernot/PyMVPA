# emacs: -*- mode: python; py-indent-offset: 4; indent-tabs-mode: nil -*-
# vi: set ft=python sts=4 ts=4 sw=4 et:
### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
#
#   See COPYING file distributed along with the PyMVPA package for the
#   copyright and license terms.
#
### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
'''
General AFNI NIML I/O support

Created on Feb 16, 2012

@author: Nikolaas. N. Oosterhof (nikolaas.oosterhof@unitn.it)

This function reads a NIML file and returns a dict that contains all 
NIML information in a tree-like structure (dicts for which some values
are dicts themselves). Branches are stored in a 'nodes' field.

For specific types of data, consider afni_niml_annot or afni_niml_annot
files which provide easier access to the data.

WiP

TODO: some nice refactoring of the code. Currently it's a bit of
      a mess.
'''

import re, numpy as np, random, os, time, sys, base64, copy, math
from io import BytesIO

from mvpa2.support.nibabel import afni_niml_types as types
_RE_FLAGS = re.DOTALL # regular expression matching spans across new lines

from mvpa2.base import debug
if __debug__:
    if not "NIML" in debug.registered:
        debug.register("NIML", "NeuroImaging Markup Language")

_TEXT_ROWSEP = "\n"
_TEXT_COLSEP = " "

# define NIML specific escape characters
_ESCAPE = {'&lt;':'<',
         '&gt;':'>',
         '&quot;':'"',
         '&amp;':'&',
         '&apos;':"'"}

def decode_escape(s):
    for k, v in _ESCAPE.iteritems():
        s = s.replace(k, v)
    return s

def encode_escape(s):
    for k, v in _ESCAPE.iteritems():
        s = s.replace(v, k)
    return s

def _parse_keyvalues(s):
    '''parse K0=V0 K1=V1 ... and return a dict(K0=V0,K1=V1,...)'''

    e = b'\s*(?P<lhs>\w+)\s*=\s*"(?P<rhs>[^"]+)"'

    m = re.findall(e, s, _RE_FLAGS)
    return dict([(k.decode(), v.decode()) for k, v in m])

def _mixedtypes_datastring2rawniml(s, niml):
    tps = niml['vec_typ']
    ncols = len(tps)
    nrows = niml['vec_len']

    lines = s.strip().split(_TEXT_ROWSEP)
    if len(lines) != nrows:
        raise ValueError("Expected %d rows, but found %d" % (nrows, len(lines)))

    elems = map(lambda x : x.strip().split(_TEXT_COLSEP), lines)
    fs = map(types.code2python_convertor, tps)

    data = []
    for col in xrange(ncols):
        f = fs[col]
        if types.sametype(tps[col], 'String'):
            d = map(f, [elems[r][col] for r in xrange(nrows)])
        else:
            tp = types.code2numpy_type(tps[col])
            niform = niml.get('ni_form', None)
            if not niform is None:
                raise ValueError('Not supported: have ni_form with mixed types')

            d = np.zeros((nrows,), dtype=tp) # allocate one-dimensional array
            for r in xrange(nrows):
                d[r] = f(elems[r][col])

        data.append(d)

    return data


def _datastring2rawniml(s, niml):
    debug('NIML', 'Raw string to NIML: %d characters', len(s))

    tps = niml['vec_typ']

    onetype = types.findonetype(tps)

    if onetype is None:
        return _mixedtypes_datastring2rawniml(s, niml)

    if [onetype] == types.str2codes('string'):
        return decode_escape(s.decode()) # do not string2rawniml

    # numeric, either int or float
    ncols = niml['vec_num']
    nrows = niml['vec_len']
    tp = types.code2numpy_type(onetype)

    niform = niml.get('ni_form', None)

    if not niform or niform == 'text':
        data = np.zeros((nrows, ncols), dtype=tp) # allocate space for data 
        convertor = types.code2python_convertor(onetype) # string to type convertor 

        vals = s.split(None) # split by whitespace seperator
        if len(vals) != ncols * nrows:
            raise ValueError("unexpected number of elements")

        for i, val in enumerate(vals):
            data[i / ncols, i % ncols] = convertor(val)

    else:
        dtype = np.dtype(tp)
        dtype = types.byteorder_from_niform(niform, dtype)

        if 'base64' in niform:
            debug('NIML', 'base64, %d chars: %s',
                            (len(s), _partial_string(s, 0)))

            s = base64.b64decode(s)
        elif not 'binary' in niform:
            raise ValueError('Illegal niform %s' % niform)

        data_1d = np.fromstring(s, dtype=tp)

        debug('NIML', 'data vector has %d elements, reshape to %d x %d = %d',
                        (np.size(data_1d), nrows, ncols, nrows * ncols))

        data = np.reshape(data_1d, (nrows, ncols))

    return data

def getnewidcode():
    return ''.join(map(chr, [random.randint(65, 65 + 25) for _ in xrange(24)]))

def setnewidcode(s):
    tp = type(s)
    if tp is list:
        for v in s:
            setnewidcode(v)
    elif tp is dict:
        key = 'self_idcode'
        for k, v in s.iteritems():
            if k == key:
                s[key] = getnewidcode()
            else:
                setnewidcode(v)


def rawniml2string(p, form='text'):
    if type(p) is list:
        nb = '\n'.encode()
        return nb.join(rawniml2string(v, form) for v in p)

    if not form in ['text', 'binary', 'base64']:
        raise ValueError("Illegal form %s" % form)

    q = p.copy() # make a shallow copy


    if 'nodes' in q:
        s_body = rawniml2string(q.pop('nodes'), form) # recursion
    else:
        data = q.pop('data')
        data = types.nimldataassupporteddtype(data) # ensure the data format is supported by NIML
        s_body = _data2string(data, form)

        if form == 'text':
            q.pop('ni_form', None) # defaults to text, remove if already there
        else:
            byteorder = types.data2ni_form(data, form)
            if byteorder:
                q['ni_form'] = byteorder

        # remove some unncessary fields
        for f in ['vec_typ', 'vec_len', 'vec_num']:
            q.pop(f, None)

    s_name = q.pop('name', None).encode()
    s_header = _header2string(q)

    d = map(lambda x:x.encode(), ['<', '\n', ' >', '</', '>'])
    return b''.join((d[0], s_name, d[1], s_header, d[2], s_body, d[3], s_name, d[4]))

def _data2string(data, form):
    if isinstance(data, basestring):
        return ('"%s"' % encode_escape(data)).encode()

    elif type(data) is np.ndarray:
        if form == 'text':
            f = types.numpy_data2printer(data)
            nrows, ncols = data.shape
            return _TEXT_ROWSEP.join([_TEXT_COLSEP.join([f(data[row, col])
                                                         for col in xrange(ncols)])
                                                         for row in xrange(nrows)]).encode()
        elif form == 'binary':
            data_reshaped = data.reshape((data.shape[1], data.shape[0]))
            r = data_reshaped.tostring()
            debug('NIML', 'Binary encoding (len %d -> %d): [%s]' %
                            (data_reshaped.size, len(r), _partial_string(r, 0)))
            return r
        elif form == 'base64':
            data_reshaped = data.reshape((data.shape[1], data.shape[0]))
            r = base64.b64encode(data_reshaped.tostring())
            debug('NIML', 'Encoding ok: [%s]', _partial_string(r, 0))
            return r
        else:
            raise ValueError("illegal format %s" % format)

    elif type(data) is list:
        # mixed types, each column in its own container
        # always use text output format, even if requested form is binary of base64

        ncols = len(data)
        if ncols == 0:
            return "".encode()
        else:
            nrows = len(data[0])

            # separate formatter functions for each column
            # if list of strings then take first element of the list to get a string formattr
            # else use the entire np array to get a numeric formatter
            fs = [types.numpy_data2printer(d[0] if type(d) is list else d) for d in data]

            return _TEXT_ROWSEP.join([_TEXT_COLSEP.join([fs[col](data[col][row])
                                                         for col in xrange(ncols)])
                                                         for row in xrange(nrows)]).encode()

    else:
        raise TypeError("Unknown type %r" % type(data))

def _header2string(p, keyfirst=['dset_type', 'self_idcode', 'filename', 'data_type'], keylast=['ni_form']):
    otherkeys = list(set(p.keys()) - (set(keyfirst) | set(keylast)))

    added = set()
    keyorder = [keyfirst, otherkeys, keylast]
    kvs = []
    for keys in keyorder:
        for k in keys:
            if k in p and not k in added:
                kvs.append((k, p[k]))
                added.add(k)

    rs = map(lambda x : '   %s="%s"' % x, kvs)
    return ("\n".join(rs)).encode()

def read(fn, itemifsingletonlist=True, postfunction=None):
    import io
    with io.FileIO(fn) as f:
        s = f.read()
    #with open(fn) as f:
    #    s = f.read()

    r = string2rawniml(s)
    if not postfunction is None:
        r = postfunction(r)

    if itemifsingletonlist and type(r) is list and len(r) == 1:
        return r[0]
    else:
        return r

def _partial_string(s, i, maxlen=100):

    # length of the string to print
    n = len(s) - i
    if n <= 0 or maxlen == 0:
        return '' # nothing to print

    if maxlen < 0 or maxlen > n:
        maxlen = n # print the whole string
    elif maxlen > n:
        maxlen = n

    # half the size of a segment
    startsize = maxlen // 2
    stopsize = startsize + maxlen % 2

    infix = ' ... ' if n > maxlen else ''

    return '%s%s%s' % (s[i:(i + startsize)], infix, s[-stopsize:])

def string2rawniml(s, i=None):
    '''Parses a NIML string to a raw NIML tree-like structure
    
    Parameters
    ----------
    s: str
        string to be converted
    i: int
        Starting position in the string.
        By default None is used, which means that the entire string is 
        converted.
        
    Returns
    -------
    r: the NIML result.
        If input parameter i is None then a dictionary with NIML elements, or 
        a list containing such elements, is returned. If i is an integer, 
        then a tuple j, d is returned with d the new starting position and a 
        dictionary or list with the elements parsed so far.
    '''

    # return new starting position?
    return_pos = not i is None
    if not return_pos:
        i = 0

    debug('NIML', 'Parsing at %d, total length %d', (i, len(s)))
    # start parsing from header
    #
    # the tricky part is that binary data can contain characters that also 
    # indicate the end of a data segment, so 'typical' parsing with start
    # and end markers cannot be done. Instead the header of each part is
    # read first, then the number of elements is computed based on the 
    # header information, and the required number of bytes is converted.
    # From then on the remainder of the string is parsed as above.


    headerpat = b'\W*<(?P<name>\w+)\W(?P<header>.*?)>'

    nimls = [] # here all found parts are stored

    # Keep on reading new parts
    while True:
        # ignore any xml tags
        if s.startswith('<?xml', i):
            i = s.index('>', i) + 1

        # try to read a name and header part
        m = re.match(headerpat, s[i:], _RE_FLAGS)

        if m is None:
            # no header - was it the end of a section?
            m = re.match(b'\W*</\w+>\s*', s[i:], _RE_FLAGS)

            if not m is None:
                # for NIFTI extensions there can be some null bytes left
                # so get rid of them here
                remaining = s[i + m.end():].replace(chr(0), '').strip()

                if len(remaining) == 0:
                # entire file was parsed - we are done
                    debug('NIML', 'Completed parsing, length %d (%d elements)', (len(s), len(nimls)))
                    if return_pos:
                        return i, nimls
                    else:
                        return nimls

            # not good - not at the end of the file
            raise ValueError("Unexpected end: [%s] " % _partial_string(s, i))

        else:
            # get values from header
            d = m.groupdict()
            name, header = d['name'], d['header']

            # update current position
            i += m.end()

            # parse the keys and values in the header
            debug('NIML', 'Parsing header %s, header end position %d',
                                                (name, i + m.end()))
            niml = _parse_keyvalues(header)

            debug('NIML', 'Found keys %s.', (", ".join(niml.keys())))
            # set the name of this element
            niml['name'] = name.decode()

            if niml.get('ni_form', None) == 'ni_group':
                # it's a group. Parse the group using recursion
                debug("NIML", "Starting a group %s >>>" , niml['name'])
                i, niml['nodes'] = string2rawniml(s, i)
                debug("NIML", "<<< ending a group %s", niml['name'])
            else:
                # it's a normal element with data
                debug('NIML', 'Parsing element %s from position %d, total '
                                    'length %d', (niml['name'], i, len(s)))

                # set a few data elements
                datatypes = niml['ni_type']
                niml['vec_typ'] = types.str2codes(datatypes)
                niml['vec_len'] = int(niml['ni_dimen'])
                niml['vec_num'] = len(niml['vec_typ'])

                # data can be in string form, binary or base64.
                is_string = niml['ni_type'] == 'String' or \
                                not 'ni_form' in niml
                if is_string:
                    # string form is handled separately. It's easy to parse 
                    # because it cannot contain any end markers in the data 

                    debug("NIML", "Parsing string body for %s", name)

                    is_string_data = niml['ni_type'] == 'String'

                    # If the data type is string, it is surrounded by quotes
                    # Otherwise (numeric data) there are no quotes
                    quote = '"' if is_string_data else ''

                    # construct the regular pattern for this string
                    strpat = ('\s*%s(?P<data>[^"]*)[^"]*%s\s*</%s>' % \
                                                    (quote, quote, name.decode())).encode()
                    m = re.match(strpat, s[i:])
                    if m is None:
                        # something went wrong
                        raise ValueError("Could not parse string data from "
                                         "pos %d: %s" %
                                                (i, _partial_string(s, i)))

                    # parse successful - get the parsed data
                    data = m.groupdict()['data']


                    # convert data to raw NIML
                    data = _datastring2rawniml(data, niml)

                    # if string data, replace esscape characters                    
                    if is_string_data:
                        data = decode_escape(data)

                    # store data
                    niml['data'] = data

                    # update position
                    i += m.end()

                    debug('NIML', 'Completed %s, now at %d', (name, i))

                else:
                    # see how many bytes (characters) to read

                    # convert this part of the string
                    if 'base64' in niml['ni_form']:
                        # base 64 has no '<' character - so we should be fine
                        endpos = s.index(b'<', i + 1)
                        datastring = s[i:endpos]
                        nbytes = len(datastring)
                    else:
                        # hardcode binary data - see how many bytes we need
                        nbytes = _binary_data_bytecount(niml)
                        debug('NIML', 'Raw data with %d bytes - total length '
                                    '%d, starting at %d', (nbytes, len(s), i))
                        datastring = s[i:(i + nbytes)]

                    niml['data'] = _datastring2rawniml(datastring, niml)

                    # update position
                    i += nbytes

                    # ensure that immediately after this segment there is an 
                    # end-part marker
                    endstr = '</%s>' % name.decode()
                    if s[i:(i + len(endstr))].decode() != endstr:
                        raise ValueError("Not found expected end string %s"
                                         "  (found %s...)" %
                                            (endstr, _partial_string(s, i)))
                    i += len(endstr)

            debug('NIML', "Adding element '%s' with keys %r" % (niml['name'], niml.keys()))
            nimls.append(niml)

    # we should never end up here.
    raise ValueError("this should never happen")


def _binary_data_bytecount(niml):
    '''helper function that returns how many bytes a NIML binary data
    element should have'''
    niform = niml['ni_form']
    if not 'binary' in niform:
        raise ValueError('Illegal niform %s' % niform)

    tps = niml['vec_typ']
    onetype = types.findonetype(tps)

    if onetype is None:
        debug('NIML', 'Not unique type: %r', tps)
        return None

    # numeric, either int or float
    ncols = niml['vec_num']
    nrows = niml['vec_len']
    tp = types.code2numpy_type(onetype)
    bytes_per_elem = types.numpy_type2bytecount(tp)

    if bytes_per_elem is None:
        raise ValueError("Type not supported: %r" % onetype)

    nb = ncols * nrows * bytes_per_elem

    debug('NIML', 'Number of bytes for %s: %d x %d with %d bytes / element',
                                    (niform, ncols, nrows, bytes_per_elem))

    return nb


def write(fnout, niml, form='binary', prefunction=None):
    if not prefunction is None:
        niml = prefunction(niml)

    s = rawniml2string(niml, form=form)

    import io
    with io.FileIO(fnout, 'w') as f:
        n = f.write(s)
    if n != len(s):
        raise ValueError("Not all bytes written to %s" % fnout)
