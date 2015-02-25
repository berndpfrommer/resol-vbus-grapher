#!/usr/bin/env python
# -----------------------------------------------------------------------------
# program to read and process Resol VBus data
#

import sys
import time
import socket
import errno
import select
import struct
import ConfigParser
import logging
import logging.config
import rrdtool
import os.path
import re
import shlex
import subprocess
from signal import SIGTERM

try: import readline  # For readline input support
except: pass

import sys, os, traceback, signal, codeop, cStringIO, cPickle, tempfile
def pipename(pid):
    """Return name of pipe to use"""
    return os.path.join(tempfile.gettempdir(), 'debug-%d' % pid)

class NamedPipe(object):
    def __init__(self, name, end=0, mode=0666):
        """Open a pair of pipes, name.in and name.out for communication
        with another process.  One process should pass 1 for end, and the
        other 0.  Data is marshalled with pickle."""
        self.in_name, self.out_name = name +'.in',  name +'.out',
        try: os.mkfifo(self.in_name,mode)
        except OSError: pass
        try: os.mkfifo(self.out_name,mode)
        except OSError: pass
        
        # NOTE: The order the ends are opened in is important - both ends
        # of pipe 1 must be opened before the second pipe can be opened.
        if end:
            self.inp = open(self.out_name,'r')
            self.out = open(self.in_name,'w')
        else:
            self.out = open(self.out_name,'w')
            self.inp = open(self.in_name,'r')
        self._open = True

    def is_open(self):
        return not (self.inp.closed or self.out.closed)
        
    def put(self,msg):
        if self.is_open():
            data = cPickle.dumps(msg,1)
            self.out.write("%d\n" % len(data))
            self.out.write(data)
            self.out.flush()
        else:
            raise Exception("Pipe closed")
        
    def get(self):
        txt=self.inp.readline()
        if not txt: 
            self.inp.close()
        else:
            l = int(txt)
            data=self.inp.read(l)
            if len(data) < l: self.inp.close()
            return cPickle.loads(data)  # Convert back to python object.
            
    def close(self):
        self.inp.close()
        self.out.close()
        try: os.remove(self.in_name)
        except OSError: pass
        try: os.remove(self.out_name)
        except OSError: pass

    def __del__(self):
        self.close()
        
def remote_debug(sig,frame):
    """Handler to allow process to be remotely debugged."""
    def _raiseEx(ex):
        """Raise specified exception in the remote process"""
        _raiseEx.ex = ex
    _raiseEx.ex = None
    
    try:
        # Provide some useful functions.
        locs = {'_raiseEx' : _raiseEx}
        locs.update(frame.f_locals)  # Unless shadowed.
        globs = frame.f_globals
        
        pid = os.getpid()  # Use pipe name based on pid
        pipe = NamedPipe(pipename(pid))
    
        old_stdout, old_stderr = sys.stdout, sys.stderr
        txt = ''
        pipe.put("Interrupting process at following point:\n" + 
               ''.join(traceback.format_stack(frame)) + ">>> ")
        
        try:
            while pipe.is_open() and _raiseEx.ex is None:
                line = pipe.get()
                if line is None: continue # EOF
                txt += line
                try:
                    code = codeop.compile_command(txt)
                    if code:
                        sys.stdout = cStringIO.StringIO()
                        sys.stderr = sys.stdout
                        exec code in globs,locs
                        txt = ''
                        pipe.put(sys.stdout.getvalue() + '>>> ')
                    else:
                        pipe.put('... ')
                except:
                    txt='' # May be syntax err.
                    sys.stdout = cStringIO.StringIO()
                    sys.stderr = sys.stdout
                    traceback.print_exc()
                    pipe.put(sys.stdout.getvalue() + '>>> ')
        finally:
            sys.stdout = old_stdout # Restore redirected output.
            sys.stderr = old_stderr
            pipe.close()

    except Exception:  # Don't allow debug exceptions to propogate to real program.
        traceback.print_exc()
        
    if _raiseEx.ex is not None: raise _raiseEx.ex
    
def debug_process(pid):
    """Interrupt a running process and debug it."""
    os.kill(pid, signal.SIGUSR1)  # Signal process.
    pipe = NamedPipe(pipename(pid), 1)
    try:
        while pipe.is_open():
            txt=raw_input(pipe.get()) + '\n'
            pipe.put(txt)
    except EOFError:
        pass # Exit.
    pipe.close()

def listen():
    signal.signal(signal.SIGUSR1, remote_debug) # Register for remote debugging.


#
# python 2.4 does not have defaultdict
#
try:
    from collections import defaultdict
except:
    class defaultdict(dict):
        def __init__(self, default_factory=None, *a, **kw):
            if (default_factory is not None and
                not hasattr(default_factory, '__call__')):
                raise TypeError('first argument must be callable')
            dict.__init__(self, *a, **kw)
            self.default_factory = default_factory
        def __getitem__(self, key):
            try:
                return dict.__getitem__(self, key)
            except KeyError:
                return self.__missing__(key)
        def __missing__(self, key):
            if self.default_factory is None:
                raise KeyError(key)
            self[key] = value = self.default_factory()
            return value
        def __reduce__(self):
            if self.default_factory is None:
                args = tuple()
            else:
                args = self.default_factory,
            return type(self), args, None, None, self.items()
        def copy(self):
            return self.__copy__()
        def __copy__(self):
            return type(self)(self.default_factory, self)
        def __deepcopy__(self, memo):
            import copy
            return type(self)(self.default_factory,
                              copy.deepcopy(self.items()))
        def __repr__(self):
            return 'defaultdict(%s, %s)' % (self.default_factory,
                                            dict.__repr__(self))

#
# dump stack trace when receiving SIGUSR1
#
def debug(sig, frame):
    """Interrupt running process, and provide a python prompt for
    interactive debugging."""
    d={'_frame':frame}         # Allow access to frame object.
    d.update(frame.f_globals)  # Unless shadowed by global
    d.update(frame.f_locals)

    i = code.InteractiveConsole(d)
    message  = "Signal received : entering python shell.\nTraceback:\n"
    message += ''.join(traceback.format_stack(frame))
    i.interact(message)

def listen():
    signal.signal(signal.SIGUSR1, debug)  # Register handler


class SocketBuffer:
    def __init__(self, socket):
        self.buf = ()
        self.socket = socket
        self.skipData = True  # skip first until sync byte is found

class Msg:

    s_toAddr = {}
    s_toProtocol = {0x10:"V1", 0x20:"V2", 0x30:"V3"}
    s_toCommand  = {0x10:{0x0100:"SLV_DATA", 0x0200:"SLV_DATA_ANSW_REQUIRED",
                          0x0300:"SLV_ANSWER_REQUEST"},
                    0x20:{0x0100:"ANSWER", 0x0200:"WRITE_VAL_ACK_REQUIRED",
                          0x0300:"READ_VAL_ACK REQUIRED",
                          0x0400:"WRITE_VAL_ACK REQUIRED",
                          0x0500:"MAS_VBUS_CLEAR",
                          0x0600:"SLV_VBUS_CLEAR"}}
    @staticmethod
    def s_dumpAsHex(buffer, len):
        ss = ''
        for s in range(len):
            ss += '%02X ' % buffer[s]
        return ss

    @staticmethod
    def s_getInt16(buf):
        # code like this makes me puke...
        packed = struct.pack('BB', buf[0], buf[1])
        u = socket.ntohs(struct.unpack("!H", packed)[0])
        if u > 32767:
            u = u - 65536
        return u

    @staticmethod
    def s_getProtocol(p):
        if p not in Msg.s_toProtocol:
            return "INV"
        return Msg.s_toProtocol[p]

    @staticmethod
    def s_getCommand(p, c):
        if p not in Msg.s_toCommand:
            return "INVALID_PROTO"
        cdict = Msg.s_toCommand[p]
        if c not in cdict:
            return "INVALID_CMD"
        return cdict[c]

    @staticmethod
    def s_getAddr(a):
        if a not in Msg.s_toAddr:
            return "UNKNOWN"
        return Msg.s_toAddr[a]

    @staticmethod
    def s_checkCRC(a):
        crc = 0x7f
        if (len(a) < 1):
            raise RuntimeError('no crc provided!')
        crcidx = len(a) - 1 # crc is last byte
        for i in range (crcidx):
            crc = (crc - a[i]) & 0x7F
        return crc == a[crcidx]

    @staticmethod
    def s_restoreSeptet(a):
        datalen = len(a) - 1
        septet  = a[datalen]
        b       = list(a[:datalen])  # make copy first
        for i in range(datalen):
            b[i] = b[i] | (((1 << i) & septet) << (7 - i))
        return tuple(b)

    @staticmethod
    def s_unpackFrame(a):
        datalen = len(a) - 2 # last two bytes are septet and crc
        if (len(a) < 2):
            raise RuntimeError('incomplete frame!')
        if not Msg.s_checkCRC(a):
            raise RuntimeError('VBUS checksum error!')
        return Msg.s_restoreSeptet(a[:datalen + 1])

    @staticmethod
    def s_splitMsgName(msgname):
        ml = msgname.split('_')
        if not len(ml) == 3:
            raise RuntimeError('msg must have src_dst_cmd format: ' + msgname)
        return map(lambda x:int(x,0), ml)

    @staticmethod
    def s_unpackV1(numframes, a):
        flen = 6  # incoming frame length
        plen = 4  # outgoing payload length per frame
        if len(a) < numframes * flen:
            raise RuntimeError('V1 error: msg short')
        if len(a) > numframes * flen:
            raise RuntimeError('V1 error: msg long')
        b = [0]* numframes * 4 # list with numframes * 4 zeros
        for i in range(numframes):
            frame = a[(i * flen):(i * flen + flen)]
            if not Msg.s_checkCRC(frame):
                raise RuntimeError('V1 bad CRC for frame')
            b[(i * plen):(i * plen + plen)]=Msg.s_restoreSeptet(frame[:flen-1])
        return b

    @staticmethod
    def s_parseDecoding(msgname, decodestr):
        mi = Msg.s_splitMsgName(msgname)
        lines = list(filter(None, [x.strip() for x in decodestr.splitlines()]))
        Msg.s_decode[mi[0]][mi[1]][mi[2]] = {} # ensure empty entry if no lines
        for l in lines:
            a = l.split(',')
            if not len(a) == 6:
                raise RuntimeError('invalid decode line: ' + l)
            off = a[1].split('-')
            if not len(off) == 2:
                raise RuntimeError('offset must have n-m format: ' + l)
            Msg.s_decode[mi[0]][mi[1]][mi[2]][a[0]] =\
                     {'off':(map(lambda x:int(x), off)),'mult':float(a[2]),
                      'units':str(a[3]),'fmt':str(a[4]),'dbfmt':str(a[5])}


    @staticmethod
    def s_initDecoding(config):
        #
        # first get address -> clear text mapping
        #
        addrs = config.get('vbus', 'addresses')
        kw = addrs.split(',')
        if not kw:
            raise RuntimeError('no address decoding info in config file!')
        for a in kw:
            ad = a.split(':')
            if not len(ad) == 2:
                raise RuntimeError('bad addr in config: ' + a)
            Msg.s_toAddr[int(ad[0],0)] = ad[1]
        #
        # now the list of known messages
        msgs = config.get('vbus', 'messages')
        kw = msgs.split(',')
        if not kw:
            raise RuntimeError('bad messages keyword in vbus config file!')
        decodedict = lambda: defaultdict(decodedict)
        Msg.s_decode = decodedict()
        for m in kw:
            msg = config.get('vbus', 'msg_' + m)
            Msg.s_parseDecoding(m, msg)

    def __init__(self, data):
        self.data = data
        self.src_addr = -1
        self.dst_addr = -1
        self.version  = -1
        self.cmd      = -1
        self.decoded_data = {}
        self.analyze()

    def analyze(self):
        self.dst_addr = Msg.s_getInt16(self.data[0:2])
        self.src_addr = Msg.s_getInt16(self.data[2:4])
        self.version  = self.data[4];
        self.cmd      = Msg.s_getInt16(self.data[5:7])
        log=logging.getLogger('root')
        log.debug('msg: ' + Msg.s_getProtocol(self.version) + ' '
                  + Msg.s_dumpAsHex(self.data, len(self.data)))

        if self.version == 0x10:
            if not Msg.s_checkCRC(self.data[0:9]):
                raise RuntimeError('V1 header CRC error')
            self.unpacked = Msg.s_unpackV1(self.data[7],
                                           self.data[9:])
        elif self.version == 0x20:
            if len(self.data) > 15:
                raise RuntimeError('V2 long packet discarded!')
            if len(self.data) < 15:
                raise RuntimeError('V2 short packet discarded!')
            if not Msg.s_checkCRC(self.data):
                raise RuntimeError('V2 CRC error')
            self.unpacked = Msg.s_restoreSeptet(self.data[7:14])
        else:
            raise RuntimeError('unknown version')
        self.decode()
    def toHexString(self):
        ss = ''
        for s in range(len(self.unpacked)):
            ss += '%02X' % self.unpacked[s] + ' '
        return ss

    def decode(self):
        if not self.src_addr in Msg.s_decode or \
                not self.dst_addr in Msg.s_decode[self.src_addr] or \
                not self.cmd in Msg.s_decode[self.src_addr][self.dst_addr]:
            raise RuntimeError('cannot decode unknown msg: %x->%x cmd: %x' %
                               (self.src_addr, self.dst_addr, self.cmd))
        dc = Msg.s_decode[self.src_addr][self.dst_addr][self.cmd]
        for k, v in dc.items():
            off = v['off']
            if off[1] == 1:
                num = self.unpacked[off[0]]
            elif off[1] == 2:
                num = Msg.s_getInt16(self.unpacked[off[0]:off[0]+off[1]])
#                log=logging.getLogger('root')
#                log.debug('converted ' + Msg.s_dumpAsHex(self.unpacked[off[0]:off[0]+off[1]], 2) + ' to ' + str(num))
            else:
                raise RuntimeError('bad offset')
            num = num * v['mult']
            self.decoded_data[k] = {'value':num, 'field':v}

    def toString(self):
        if self.decoded_data:
            ds = ''
            for k, v in self.decoded_data.items():
                num = v['value']
                field = v['field']
                fstr = '%s = ' + field['fmt'] + '%s'
                ds = ds + ' ' + fstr % (k, num, field['units'])
        else:
            ds = self.toHexString()

        ss = '%16s->%8s %3s %15s data: %s' % \
            (Msg.s_getAddr(self.src_addr), Msg.s_getAddr(self.dst_addr),
             Msg.s_getProtocol(self.version),
             Msg.s_getCommand(self.version, self.cmd),
             ds)
        return ss



def usage():
    print "usage: resol.py config_file_name"

def blockingConnect(hostName, port):
    log=logging.getLogger('root')
    log.info('connecting to ' + hostName + ':' + str(port))
    connected = False
    wait = 5
    while (not connected):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect((hostName, port))
        except (socket.herror, socket.error):
            log.critical('error connecting to ' + hostName + ':' + str(port))
            log.info('will retry in ' + str(wait) + ' seconds')
            s.close()
            time.sleep(wait)
            continue
        connected = True

    log.info('connected to ' + hostName + ':' + str(port))
    return s

def waitForReply(s, expectedMsg):
    log=logging.getLogger('root')
    msg = ''
    while len(msg) < len(expectedMsg):
        chunk = s.recv(len(expectedMsg) - len(msg))
        if (chunk == ''):
                raise RuntimeError('socket read error')
        msg = msg + chunk
    if (not (msg == expectedMsg)):
        
        raise RuntimeError('unexpected reply: ' + msg)
    log.debug('got reply ' + expectedMsg)

def sendMsg(s, msg):
    i = 0
    while i < len(msg):
        sent = s.send(msg[i:])
        if sent == 0:
            raise RuntimeError('socket write error')
        i += sent

def establishStream(s):
    #
    # sample session: telnet resol 7053
    # 
    #+HELLO
    #PASS vbus
    #+OK: Password accepted
    #DATA
    log=logging.getLogger('root')
    log.info('establishing stream')
    waitForReply(s, '+HELLO\n')
    sendMsg(s, 'PASS vbus\n')
    waitForReply(s, '+OK: Password accepted\n')
    sendMsg(s, 'DATA\n')
    waitForReply(s, '+OK: Data incoming...\n')

def readData(s, msgs):
    maxReadSize = 10000
#    buf = bytearray(maxReadSize + 1)
#    view = memoryview(buf)
#    nread = s.socket.recv_into(view, maxReadSize)
    buf = s.socket.recv(maxReadSize)
    nread = len(buf)
    buf = tuple(struct.unpack('B',k)[0] for k in buf)
    log=logging.getLogger('root')
    log.debug('read length: ' + str(nread) + ' data: ' +
               Msg.s_dumpAsHex(buf, nread))
    log.debug('stored from prev read: ' + Msg.s_dumpAsHex(s.buf, len(s.buf)))
    firstunconsumed = 0  # first unconsumed byte in newly read buffer
    for i in range(nread):
        if s.skipData: # in skip mode, searching for next sync byte 0xAA
            firstunconsumed = i + 1
            if buf[i] == 0xAA: # got sync byte, done skipping
                s.skipData = False
        else:
            if buf[i] == 0xAA: # found sync byte AA:
                msgbuf = s.buf + buf[firstunconsumed:i]
                if len(msgbuf) > 1: # we have a complete msg
                    try:
                        nm = Msg(msgbuf) # may raise exception
                        msgs.append(nm)
                    except RuntimeError, e:
                        log.warn('malformed msg: ' + e.args[0])
                firstunconsumed = i + 1 # skip sync byte
                s.buf = () # msgs from previous packet have been consumed
            elif (buf[i] & 0x80) != 0: # found byte that renders msg invalid
                log.warn('found byte with bit 7 set')
                s.buf = () # discard all old data
                firstunconsumed = i + 1
                s.skipData = True # skip all data until next sync byte
                
    # unconsumed bytes need to be remembered
    pbuf = buf[firstunconsumed:nread]
    log.debug ('remember for next use: ' + str(firstunconsumed) + '-' + \
                   str(nread - 1) + ' content: ' + \
                   Msg.s_dumpAsHex(pbuf, len(pbuf)))
    s.buf = s.buf + buf[firstunconsumed:nread]
    return nread

def make_rrd_database(rrdsection, config, recordValues):
    log=logging.getLogger('root')
    step = 10 # basic time step is 10 seconds
    rras = {
        # 10 sec data for a month
        'short':{'cf':'LAST', 't':3600*24*30, 'bin':step},
        # 5 min data for a year
        'medium':{'cf':'AVERAGE', 't':3600*24*365, 'bin':300},
        # 30 min data for 5 years
        'long':{'cf':'AVERAGE', 't':3600*24*365*5, 'bin':1800}}
    hb   = 2 * step
    fname = config.get(rrdsection, 'rrd_filename')
    rrdstr = [fname, "--step", str(step),
            "DS:DUMMY:GAUGE:" + str(hb) + ":U:U"]
               
    for t, rra in rras.items():
        nsteps = rra['bin'] / step
        nrows  = rra['t']   / rra['bin']
        rrdstr += ["RRA:%s:0.5:%d:%d" % (rra['cf'], nsteps, nrows)]
    
    # if no database is there, create it
    if not os.path.isfile(fname):
        log.info('creating rrd database file ' + fname)
        js = 'rrdtool create ' + ' '.join(rrdstr)
        pipe = subprocess.call(js.split())

    process = subprocess.Popen(["rrdtool","info", fname],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    
    info, err = process.communicate() # returns when process complete
    info = info.splitlines()
    dsl = set()
    for ds in filter(lambda k: 'ds[' in k, info):
        a = re.split('\[|\]', ds)
        dsl.add(a[1])
    allrv = set()
    for i in recordValues.keys():
        for j in recordValues[i]:
            for k in recordValues[i][j]:
                for l in recordValues[i][j][k]:
                    allrv.add(l)
    diff = allrv - dsl # set difference!
    for d in diff:
        log = logging.getLogger('root')
        log.info('adding new datasource ' + d + ' to database')
        pipe = subprocess.call(["perl", "add_ds.pl", fname, d, 'GAUGE'])

def getRecordValues(config, recordValues):
    msgs = config.get('rrd', 'record_msgs')
    for m in msgs.split(','):
        rv = filter(None,config.get('rrd', 'record_' + m).split(','))
        mi = Msg.s_splitMsgName(m)
        recordValues[mi[0]][mi[1]][mi[2]] = rv

def processMsg(fname, msg, recordValues):
    if not msg.src_addr in recordValues.keys():
        raise RuntimeError('unknown src addr')
    rv1 = recordValues[msg.src_addr]
    if not msg.dst_addr in rv1.keys():
        raise RuntimeError('unknown dst addr')
    rv2 = rv1[msg.dst_addr]
    if not msg.cmd in rv2.keys():
        raise RuntimeError('unknown cmd')
    rv = rv2[msg.cmd] # list of values to read
    ds=[]
    vals=[]
    for k in rv:
        if not (k in msg.decoded_data.keys()):
            raise RuntimeError('dont have field: ' + k)
        ds.append(k)
        dd = msg.decoded_data[k]
        vals.append(dd['field']['dbfmt'] % dd['value'])
        
    if ds:
        rrdtool.update(fname, '-t' + str(':').join(ds),
                       'N:'+ str(':').join(vals))

def initGraphs(fname, section, config, graphs):
    log = logging.getLogger('root')
    log.info('initializing graphs')
    time_to_sec = {'HOUR':3600,
                   'DAY':3600*24,
                   'WEEK':3600*24*7,
                   'MONTH':3600*24*31,
                   'QUARTER':3600*24*91,
                   'YEAR':3600*24*365}
    for g in config.get(section,'graphs').split(','):
        defline = []
        pline = []
        timespan = time_to_sec[config.get(g,'timespan')]
        for p in config.get(g, 'plot').split(','):
            a = p.split(':')
            defline.append("DEF:%s=%s:%s:%s" % (a[0], fname, a[0], a[1]))
            pline.append("LINE%.1f:%s#%s:\'%s\'%s" %
                         (float(a[4]),a[0], a[3], a[2],
                          ("", ":" + a[5])[a[5] == ""]))
        
        graphs[g] = {'timespan':timespan,
                     'dt':timespan / 50,
                     'last_time':0,
                     'file_name':config.get(section, 'graph_output_dir') +\
                         '/graph_'+g+'.png',
                     'width':config.get(section, 'graph_width'),
                     'height':config.get(section, 'graph_height'),
                     'title':config.get(g,'title'),
                     'units':config.get(g,'units'),
                     'defline':defline,
                     'pline':pline}
        

def makeGraphs(graphs):
    log=logging.getLogger('root')
    t = time.time()
    for gn, g in graphs.items():
        if t > g['last_time'] + g['dt']: # only make if expired
            log.debug('updating graph %s', gn)
            js = "rrdtool graph %s --imgformat PNG --width %d "\
                "--height %d --start -%d --end -1 --vertical-label"\
                " %s --title \"%s\" %s %s"\
                % (g['file_name'], int(g['width']), int(g['height']),\
                       g['timespan'], g['units'], g['title'],\
                       ' '.join(g['defline']), ' '.join(g['pline']))
            log.info('graph command: %s' % js)
            process = subprocess.Popen(shlex.split(js),
                                       stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE)
            out, err = process.communicate()
            g['last_time'] = t


# ---------------------- start of main code ------------------------
#
def main():
    if len(sys.argv) != 2:
        usage()
        sys.exit(-1)
    #
    # configure the logger
    #
    logging.config.fileConfig(sys.argv[1])
    log = logging.getLogger('root')
    #
    # read config file
    #
    configFile = sys.argv[1]
    config = ConfigParser.RawConfigParser()
    config.read(configFile)

    adapterSection = 'vbus_adapter'
    if not config.has_section(adapterSection):
        log.critical('cannot find section ' + adapterSection +
                     ' in config file ' + configFile)
        sys.exit(-1)
    #
    # find out what values to record
    #
    recorddict = lambda: defaultdict(recorddict)
    recordValues = recorddict()
    getRecordValues(config, recordValues)
    #
    # prepare rrd database
    #
    rrdsection = 'rrd'
    make_rrd_database(rrdsection, config, recordValues)
    rrdfilename = config.get(rrdsection, 'rrd_filename')
    #
    # prepare graphs
    #
    graphs = {}
    initGraphs(rrdfilename, 'graph', config, graphs)
    #
    # open socket connection to vbus host
    #
    hostName  = config.get(adapterSection, 'hostname')
    port      = config.getint(adapterSection, 'port')
    Msg.s_initDecoding(config)
    timeout   = 10  # timeout in seconds
    connected = False
    reconn_sleep_time = 10 # seconds before reconnect is attempted
    traffic_check_interval = 60 # how often to check if there is still traffic
    traffic_min_msgs = 1 # number of messages expected within check interval
    log_time_interval = int(config.get('logger_root', 'log_time_interval'))

    sock_readers = []
    buffers = {}
    last_log_time    = time.time()
    last_check_time  = time.time()
    nmsgs = 0  # for logging
    ntmsgs = 0 # for traffic measurement

    while (1):
        if (not connected):
            s = blockingConnect(hostName, port)
            try:
                establishStream(s)
                connected = True
                s.setblocking(0)
                sock_readers = [s]
                buffers = {s.fileno():SocketBuffer(s)}
                log.info('connection established!')
            except socket.error, e:
                log.warn('connect failed: %s, will retry!', e.args[0])
                time.sleep(reconn_sleep_time)
                sock_readers = []

        sock_read, sock_write, sock_err = \
            select.select(sock_readers, (), (), timeout)
        for rsock in sock_read:
            if (rsock.fileno() not in buffers):
                log.critical('cannot find socket buffer, internal error')
                sys.exit(-1)
            msgs = []
            nread = 0
            try:
                nread = readData(buffers[rsock.fileno()], msgs)
                if nread == 0:
                    log.warn('socket closed!!!')
                    raise socket.error('Socket closed!')
                while nread > 0:
                    nread = readData(buffers[rsock.fileno()], msgs)
            except socket.error, e:
                if not e.args[0] == errno.EWOULDBLOCK:
                    log.warn('lost connection, trying to reestablish!')
                    rsock.close()
                    time.sleep(reconn_sleep_time)
                    connected = False

            for msg in msgs:
                log.debug(msg.toString())
                processMsg(rrdfilename, msg, recordValues)
            makeGraphs(graphs)
            nmsgs += len(msgs)
            ntmsgs += len(msgs)

        tc = time.time()
        if tc > last_check_time + float(traffic_check_interval):
            if ntmsgs < traffic_min_msgs:
                log.warn('got insufficient traffic: %d/%d, disconnecting!' %
                         (ntmsgs, traffic_min_msgs))
                rsock.close()
                time.sleep(reconn_sleep_time)
                connected = False
            last_check_time = tc
            ntmsgs = 0

        t = time.time()
        if t > last_log_time + float(log_time_interval):
            log.info('processed %4d messages in %f sec' % 
                    (nmsgs, log_time_interval))
            last_log_time = t
            nmsgs = 0

def daemonize(home_dir, out_log, pidfile):
    try:
        pf = file(pidfile, 'r')
        pid = int(pf.read().strip())
        pf.close()
    except IOError:
        pid = None

    if pid:
        message = "pidfile %s already exist. " + \
                  "Will try killing existing proc\n";
        sys.stderr.write(message % pidfile)
        try:
            while 1:
                os.kill(pid, SIGTERM)
                time.sleep(0.1)
        except OSError, err:
            err = str(err)
            if err.find("No such process") > 0:
                if os.path.exists(pidfile):
                    sys.stderr.write("killed proc %d, removing pidfile" % pid)
                    os.remove(pidfile)
            else:
                sys.stderr.write("cannot kill proc: %d err %s" % (pid, err))
                sys.exit(1)

        
    # First fork
    try:
        if os.fork() > 0:
            sys.exit(0)
    except OSError, e:
        sys.stderr.write('fork #1 failed" (%d) %s\n' %
                         (e.errno, e.strerror))
        sys.exit(1)
        
    os.setsid()
    os.chdir(home_dir)
    os.umask(0)

    # Second fork
    try:
        pid = os.fork()
        if pid > 0:
            # You must write the pid file here.  After the exit()
            # the pid variable is gone.
            fpid = open(pidfile, 'wb')
            fpid.write(str(pid))
            fpid.close()
            sys.exit(0)
    except OSError, e:
        sys.stderr.write('fork #2 failed" (%d) %s\n' % (e.errno, e.strerror))
        sys.exit(1)
    # redirect io
    try:
        si = open('/dev/null', 'r')
        so = open(out_log, 'a+', 0)
        os.dup2(si.fileno(), sys.stdin.fileno())
        os.dup2(so.fileno(), sys.stdout.fileno())
        os.dup2(so.fileno(), sys.stderr.fileno())
    except Exception, e:
        sys.stderr.write(str(e))
    

if __name__ == "__main__":
    configFile = sys.argv[1]
    config = ConfigParser.RawConfigParser()
    config.read(configFile)
    listen()
    home_dir = config.get('daemon', 'home_directory')
    out_log  = config.get('daemon', 'out_log')
    pidfile  = config.get('daemon', 'pid_file');
    if config.getboolean('daemon', 'run_as_daemon'):
        daemonize(home_dir, out_log, pidfile)
        
#
    main()
    log.info('exiting!')
    sys.exit(0)
