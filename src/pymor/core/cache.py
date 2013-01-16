from functools import partial
from dogpile import cache as dc
from dogpile.cache.backends.file import DBMBackend
from os.path import join
from tempfile import gettempdir
from collections import OrderedDict
from pprint import pformat
import sys
import os
from collections import deque

try:
    import cPickle as pickle
except:
    import pickle

from pymor.core.interfaces import BasicInterface
from pymor.tools import memory

DEFAULT_MEMORY_CONFIG = { "backend":'LimitedMemory', 'arguments.max_kbytes': 20000 }
SMALL_MEMORY_CONFIG = { "backend":'LimitedMemory', 'arguments.max_keys':20, 
                       'arguments.max_kbytes': 20}
DEFAULT_DISK_CONFIG = { "backend":'LimitedFile',
        "arguments.filename": join(gettempdir(), 'pymor.cache.dbm'),
        'arguments.max_keys':2000}
SMALL_DISK_CONFIG = { "backend":'LimitedFile',
        "arguments.filename": join(gettempdir(), 'pymor.small_cache.dbm'),
        'arguments.max_keys':20}

class LimitedMemoryBackend(BasicInterface, dc.api.CacheBackend):
    
    def __init__(self, argument_dict):
        '''If argument_dict contains a value for max_kbytes this the total memory limit in kByte that is enforced on the 
        internal cache dictionary, otherwise it's set to sys.maxint.
        If argument_dict contains a value for max_keys this maximum amount of cache values kept in the 
        internal cache dictionary, otherwise it's set to sys.maxlen.
        If necessary values are deleted from the cache in FIFO order.
        '''
        self.logger.debug('LimitedMemoryBackend args {}'.format(pformat(argument_dict)))
        self._max_keys = argument_dict.get('max_keys', sys.maxsize)
        self._max_bytes = argument_dict.get('max_kbytes', sys.maxint/(1024))*(1024)
        self._cache = OrderedDict()
        
    def get(self, key):
        return self._cache.get(key, dc.api.NO_VALUE)

    def print_limit(self, additional_size=0):
        self.logger.info('LimitedMemoryBackend at {}({}) keys -- {}({}) Byte'
                         .format(len(self._cache), self._max_keys, 
                                 memory.getsizeof(self._cache)/8, self._max_bytes))
    
    def _enforce_limits(self, new_value):
        additional_size = memory.getsizeof(new_value)/8
        while len(self._cache) > 0 and not (len(self._cache) <= self._max_keys 
                and (memory.getsizeof(self._cache) + additional_size)/8 <= self._max_bytes):
            self.logger.debug('shrinking limited memory cache')
            self._cache.popitem(last=False)
        
    def set(self, key, value):
        self._enforce_limits(value)
        self._cache[key] = value

    def delete(self, key):
        self._cache.pop(key)

class LimitedFileBackend(BasicInterface, DBMBackend):

    def __init__(self, argument_dict):
        '''If argument_dict contains a value for max_keys this maximum amount of cache values kept in the 
        internal cache file, otherwise its set to sys.maxlen.
        If necessary values are deleted from the cache in FIFO order.
        '''
        DBMBackend.__init__(self,argument_dict)
        self.logger.debug('LimitedFileBackend args {}'.format(pformat(argument_dict)))
        self._max_keys = argument_dict.get('max_keys', sys.maxsize)
        self._keylist_fn = self.filename + '.keys'
        try:
            self._keylist = pickle.load(self._keylist_fn)
        except:
            self._keylist = deque()
        self._enforce_limits(None)
        self.print_limit()
    
    def _dump_keylist(self):
        pickle.dump(self._keylist, open(self._keylist_fn,'wb'))
        
    def _new_key(self, key):
        self._keylist.append(key)
        self._dump_keylist()

    def get(self, key):
        return super(LimitedFileBackend, self).get(key)

    def print_limit(self, additional_size=0):
        self.logger.info('LimitedFileBackend at {}({}) keys'
                         .format(len(self._keylist), self._max_keys))
    
    def _enforce_limits(self, new_value):
        while len(self._keylist) > 0 and not (len(self._keylist) <= self._max_keys):
            self.logger.debug('shrinking limited memory cache')
            key = self._keylist.popleft()
            self.delete(key)
        
    def set(self, key, value):
        self._enforce_limits(value)
        if not key in self._keylist:
            self._new_key(key)
        super(LimitedFileBackend, self).set(key, value)

    def delete(self, key):
        super(LimitedFileBackend, self).delete(key)
        try:
            #api says this method is supposed to be idempotent
            self._keylist.remove(key)
        except ValueError:
            pass
        self._dump_keylist()

dc.register_backend("LimitedMemory", "pymor.core.cache", "LimitedMemoryBackend")
dc.register_backend("LimitedFile", "pymor.core.cache", "LimitedFileBackend")

class cached(BasicInterface):
    
    def __init__(self, function):
        self.decorated_function = function
        
    def __call__(self, im_self, *args, **kwargs):
        '''Via the magic that is partial functions returned from __get__, im_self is the instance object of the class
        we're decorating a method of and [kw]args are the actual parameters to the decorated method'''
        cache = im_self.cache_region
        keygen = im_self.keygen_generator(im_self.namespace, self.decorated_function)
        key = keygen(*args, **kwargs)
        def creator_function():
            self.logger.debug('creating new cache entry for {}.{}'
                              .format(im_self.__class__.__name__, self.decorated_function.__name__))
            return self.decorated_function(im_self, *args, **kwargs)
        return cache.get_or_create(key, creator_function, im_self.expiration_time)

    def __get__(self, instance, instancetype):
        '''Implement the descriptor protocol to make decorating instance method possible.
        Return a partial function where the first argument is the instance of the decorated instance object.
        ''' 
        return partial(self.__call__, instance)
        
        
class Cachable(object):
       
    def __init__(self, config=DEFAULT_MEMORY_CONFIG):
        self.cache_region = dc.make_region(function_key_generator = self.keygen_generator)
        self.cache_region.configure_from_config(config, '')
        self.namespace = '{}_{}'.format(self.__class__.__name__, hash(self))
        self.expiration_time = None
    
    def keygen_generator(self, namespace, function):
        '''I am the default generator function for (potentially) function specific keygens.
        I construct a key from the function name and given namespace 
        plus string representations of all positional and keyword args.
        '''
        fname = function.__name__
        namespace = str(namespace) 
        def keygen(*arg, **kwargs):
            return (namespace + "_" + fname + "_".join(str(s) for s in arg) 
                        + '__'.join(str(x) for x in kwargs.iteritems()))
        return keygen
    
    