import fcntl
import logging
import mmap
import os
import time

from datetime import datetime
from functools import wraps
from struct import pack, unpack

class MmapResource(object):
   """Resource implementation for a directly-mapped memory region."""
   def __init__(self, path):
      self.path_ = path
      self.mmap_ = None

   def __enter__(self):
      if not self.map():
         # raise the last exception from self.map()
         raise RuntimeError('failed to mmap %s' % self.path_ )
      return self

   def __exit__(self, *args):
      self.close()

   def map(self):
      assert not self.mmap_, "Resource already mapped"

      try:
         fd = os.open(self.path_, os.O_RDWR)
      except EnvironmentError:
         logging.error("failed to open file %s for mmap", self.path_)
         return False

      try:
         size = os.fstat(fd).st_size
      except EnvironmentError:
         logging.error("failed to stat file %s for mmap", self.path_)
         try:
            os.close(fd)
         except EnvironmentError:
            pass
         return False

      try:
         self.mmap_ = mmap.mmap(fd, size, mmap.MAP_SHARED,
                                mmap.PROT_READ | mmap.PROT_WRITE)
      except EnvironmentError:
         logging.error("failed to mmap file %s", self.path_)
         return False
      finally:
         try:
            # Note that closing the file descriptor has no effect on the memory map
            os.close(fd)
         except EnvironmentError:
            pass
      return True

   def close( self ):
      self.mmap_.close()
      self.mmap_ = None

   def read32( self, addr ):
      return unpack( '<L', self.mmap_[ addr : addr + 4 ] )[ 0 ]

   def write32( self, addr, value ):
      self.mmap_[ addr: addr + 4 ] = pack( '<L', value )

def sysfsFmtHex(x):
   return "0x%08x" % x

def sysfsFmtDec(x):
   return "%d" % x

def sysfsFmtStr(x):
   return str(x)

def incrange(start, stop):
   return list(range(start, stop + 1))

def flatten(nestedList):
   return [val for sublist in nestedList for val in sublist]

def klog(msg, level=2, *args):
   try:
      with open('/dev/kmsg', 'w') as f:
         f.write('<%d>arista: %s\n' % (level, msg % tuple(*args)))
   except: # pylint: disable-msg=W0702
      pass

class Retrying:
   def __init__(self, interval=1.0, delay=0.05, maxAttempts=None):
      self.interval = interval
      self.delay = delay
      self.maxAttempts = maxAttempts

   def __iter__(self):
      class Iterator:
         def __init__(self, interval, delay, maxAttempts):
            self.attempt = 0

            self.startedAt_ = datetime.now()
            self.interval_ = interval
            self.delay_ = delay
            self.maxAttempts_ = maxAttempts

         def __next__(self):
            time.sleep(self.delay_)
            if self.isExpired() or \
               self.maxAttempts_ and self.attempt >= self.maxAttempts_:
               raise StopIteration
            self.attempt += 1
            return self

         def next(self):
            return self.__next__()

         def isExpired(self):
            return self.interval_ and \
               (datetime.now() - self.startedAt_).total_seconds() > self.interval_

      return Iterator(self.interval, self.delay, self.maxAttempts)

class FileLock:
   def __init__(self, lock_file, auto_release=False):
      self.f = open(lock_file, 'w')
      self.auto_release = auto_release

   def lock(self):
      fcntl.flock(self.f, fcntl.LOCK_EX)

   def unlock(self):
      fcntl.flock(self.f, fcntl.LOCK_UN)
      self.f.close()

   def __enter__(self):
      self.lock()

   def __exit__(self, exc_type, exc_val, traceback):
      if self.auto_release:
         self.unlock()
      else:
         self.f.close()

class NoopObj(object):
   def __init__(self, *args, **kwargs):
      self.name = self.__class__.__name__
      self.classStr = '%s(%s)' % (self.name, self._fmtArgs(*args, **kwargs))
      logging.debug(self.classStr)

   def _fmtArgs(self, *args, **kwargs):
      kw = ['%s=%s' % (k, v) for k, v in kwargs.items()]
      return ', '.join(list(map(str, args)) + kw)

   def noop(self, attr):
      def wrapped(*args, **kwargs):
         funcStr = '%s(%s)' % (attr, self._fmtArgs(*args, **kwargs))
         logging.debug('%s.%s', self.classStr, funcStr)
      return wrapped

   def __getattr__(self, attr):
      return self.noop(attr)

CMDLINE_PATH = '/proc/cmdline'

cmdlineDict = {}
def getCmdlineDict():
   global cmdlineDict

   if cmdlineDict:
      return cmdlineDict

   data = {}
   with open(CMDLINE_PATH) as f:
      for entry in f.read().split():
         idx = entry.find('=')
         if idx == -1:
            data[entry] = None
         else:
            data[entry[:idx]] = entry[idx+1:]

   cmdlineDict = data
   return data

# debug flag, if enabled should use the most tracing possible
debug = False

# force simulation to be True if not on a Arista box
simulation = True

# simulation related globals
SMBus = None

def inDebug():
   return debug

def inSimulation():
   return simulation

def runningInContainer():
   # Docker containers by default have this path.
   return os.path.exists("/.dockerenv")

def simulateWith(simulatedFunc):
   def simulateThisFunc(func):
      @wraps(func)
      def funcWrapper(*args, **kwargs):
         if inSimulation():
            return simulatedFunc(*args, **kwargs)
         return func(*args, **kwargs)
      return funcWrapper
   return simulateThisFunc

def writeConfigSim(path, data):
   for filename, value in data.items():
      logging.info('writting data under %s : %r',
                   os.path.join(path, filename), value)

@simulateWith(writeConfigSim)
def writeConfig(path, data):
   for filename, value in data.items():
      try:
         filePath = os.path.join(path, filename)
         with open(filePath, 'w') as f:
            f.write(value)
      except IOError as e:
         logging.error('%s %s', path, e.strerror)

def libraryInit():
   global simulation, debug, SMBus

   cmdline = getCmdlineDict()
   if "Aboot" in cmdline:
      simulation = False

   if "arista-debug" in cmdline:
      debug = True

   if simulation:
      SMBus = type('SMBus', (NoopObj,), {})
   else:
      try:
         from smbus import SMBus
      except ImportError:
         pass

libraryInit()

