# hardware: RPlidar A2 M8

import logging
import sys
import time
import codecs
import serial
import struct
import numpy as np

from collections import namedtuple

# globals
# ------------------------------------------------

lidarPort = '/dev/ttyUSB0'
#lidarPort = '/dev/ttyUSB1'
arduinoPort = '/dev/ttyACM0'
#arduinoPort = '/dev/ttyACM1'

obstacleMap_CenterRow = 8
obstacleMap_CenterCol = 8
obstacleMap_Row_Len = 17
obstacleMap_Col_Len  = 17
# measurement[0] # bool new scan?
idx_NewScan = 0
# measurement[1] # int quality of laser
idx_QOL = 1
# measurement[2] # float angle (0 to 359.9999, I think)
# otherwise 360degree is same as 0degrees ???
idx_AngleDeg = 2
# measurement[3] # float distance in mm
idx_DistMm = 3

lidarTimer_Prev = 0
lidarTimer_Now = 0
lidarTimer_Treshold = 0


SYNC_BYTE = b'\xA5'
SYNC_BYTE2 = b'\x5A'

GET_INFO_BYTE = b'\x50'
GET_HEALTH_BYTE = b'\x52'

STOP_BYTE = b'\x25'
RESET_BYTE = b'\x40'

_SCAN_TYPE = {
    'normal': {'byte': b'\x20', 'response': 129, 'size': 5},
    'force': {'byte': b'\x21', 'response': 129, 'size': 5},
    'express': {'byte': b'\x82', 'response': 130, 'size': 84},
}

DESCRIPTOR_LEN = 7
INFO_LEN = 20
HEALTH_LEN = 3

INFO_TYPE = 4
HEALTH_TYPE = 6

# Constants & Command to start A2 motor
MAX_MOTOR_PWM = 1023
DEFAULT_MOTOR_PWM = 660
SET_PWM_BYTE = b'\xF0'

_HEALTH_STATUSES = {
    0: 'Good',
    1: 'Warning',
    2: 'Error',
}


class RPLidarException(Exception):
    '''Basic exception class for RPLidar'''

def _b2i(byte):
    '''Converts byte to integer (for Python 2 compatability)'''
    return byte if int(sys.version[0]) == 3 else ord(byte)

def _showhex(signal):
    '''Converts string bytes to hex representation (useful for debugging)'''
    return [format(_b2i(b), '#02x') for b in signal]

def _process_scan(raw):
    '''Processes input raw data and returns measurement data'''
    new_scan = bool(_b2i(raw[0]) & 0b1)
    inversed_new_scan = bool((_b2i(raw[0]) >> 1) & 0b1)
    quality = _b2i(raw[0]) >> 2
    if new_scan == inversed_new_scan:
        raise RPLidarException('New scan flags mismatch')
    check_bit = _b2i(raw[1]) & 0b1
    if check_bit != 1:
        raise RPLidarException('Check bit not equal to 1')
    angle = ((_b2i(raw[1]) >> 1) + (_b2i(raw[2]) << 7)) / 64.
    distance = (_b2i(raw[3]) + (_b2i(raw[4]) << 8)) / 4.
    return new_scan, quality, angle, distance


def _process_express_scan(data, new_angle, trame):
    new_scan = (new_angle < data.start_angle) & (trame == 1)
    angle = (data.start_angle + (
            (new_angle - data.start_angle) % 360
            )/32*trame - data.angle[trame-1]) % 360
    distance = data.distance[trame-1]
    return new_scan, None, angle, distance


class RPLidar(object):
    '''Class for communicating with RPLidar rangefinder scanners'''

    def __init__(self, port, baudrate=115200, timeout=1, logger=None):
        '''Initilize RPLidar object for communicating with the sensor.

        Parameters
        ----------
        port : str
            Serial port name to which sensor is connected
        baudrate : int, optional
            Baudrate for serial connection (the default is 115200)
        timeout : float, optional
            Serial port connection timeout in seconds (the default is 1)
        logger : logging.Logger instance, optional
            Logger instance, if none is provided new instance is created
        '''
        self._serial = None
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self._motor_speed = DEFAULT_MOTOR_PWM
        self.scanning = [False, 0, 'normal']
        self.express_trame = 32
        self.express_data = False
        self.motor_running = None
        if logger is None:
            logger = logging.getLogger('rplidar')
        self.logger = logger
        self.connect()

    def connect(self):
        '''Connects to the serial port with the name `self.port`. If it was
        connected to another serial port disconnects from it first.'''
        if self._serial is not None:
            self.disconnect()
        try:
            self._serial = serial.Serial(
                self.port, self.baudrate,
                parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
                timeout=self.timeout)
        except serial.SerialException as err:
            raise RPLidarException('Failed to connect to the sensor '
                                   'due to: %s' % err)

    def disconnect(self):
        '''Disconnects from the serial port'''
        if self._serial is None:
            return
        self._serial.close()

    def _set_pwm(self, pwm):
        payload = struct.pack("<H", pwm)
        self._send_payload_cmd(SET_PWM_BYTE, payload)

    @property
    def motor_speed(self):
        return self._motor_speed

    @motor_speed.setter
    def motor_speed(self, pwm):
        assert(0 <= pwm <= MAX_MOTOR_PWM)
        self._motor_speed = pwm
        if self.motor_running:
            self._set_pwm(self._motor_speed)

    def start_motor(self):
        '''Starts sensor motor'''
        self.logger.info('Starting motor')
        # For A1
        self._serial.setDTR(False)

        # For A2
        self._set_pwm(self._motor_speed)
        self.motor_running = True

    def stop_motor(self):
        '''Stops sensor motor'''
        self.logger.info('Stoping motor')
        # For A2
        self._set_pwm(0)
        time.sleep(.001)
        # For A1
        self._serial.setDTR(True)
        self.motor_running = False

    def _send_payload_cmd(self, cmd, payload):
        '''Sends `cmd` command with `payload` to the sensor'''
        size = struct.pack('B', len(payload))
        req = SYNC_BYTE + cmd + size + payload
        checksum = 0
        for v in struct.unpack('B'*len(req), req):
            checksum ^= v
        req += struct.pack('B', checksum)
        self._serial.write(req)
        self.logger.debug('Command sent: %s' % _showhex(req))

    def _send_cmd(self, cmd):
        '''Sends `cmd` command to the sensor'''
        req = SYNC_BYTE + cmd
        self._serial.write(req)
        self.logger.debug('Command sent: %s' % _showhex(req))

    def _read_descriptor(self):
        '''Reads descriptor packet'''
        descriptor = self._serial.read(DESCRIPTOR_LEN)
        self.logger.debug('Received descriptor: %s', _showhex(descriptor))
        if len(descriptor) != DESCRIPTOR_LEN:
            raise RPLidarException('Descriptor length mismatch')
        elif not descriptor.startswith(SYNC_BYTE + SYNC_BYTE2):
            raise RPLidarException('Incorrect descriptor starting bytes')
        is_single = _b2i(descriptor[-2]) == 0
        return _b2i(descriptor[2]), is_single, _b2i(descriptor[-1])

    def _read_response(self, dsize):
        '''Reads response packet with length of `dsize` bytes'''
        self.logger.debug('Trying to read response: %d bytes', dsize)
        while self._serial.inWaiting() < dsize:
            time.sleep(0.001)
        data = self._serial.read(dsize)
        self.logger.debug('Received data: %s', _showhex(data))
        return data

    def get_info(self):
        '''Get device information

        Returns
        -------
        dict
            Dictionary with the sensor information
        '''
        if self._serial.inWaiting() > 0:
            return ('Data in buffer, you can\'t have info ! '
                    'Run clean_input() to emptied the buffer.')
        self._send_cmd(GET_INFO_BYTE)
        dsize, is_single, dtype = self._read_descriptor()
        if dsize != INFO_LEN:
            raise RPLidarException('Wrong get_info reply length')
        if not is_single:
            raise RPLidarException('Not a single response mode')
        if dtype != INFO_TYPE:
            raise RPLidarException('Wrong response data type')
        raw = self._read_response(dsize)
        serialnumber = codecs.encode(raw[4:], 'hex').upper()
        serialnumber = codecs.decode(serialnumber, 'ascii')
        data = {
            'model': _b2i(raw[0]),
            'firmware': (_b2i(raw[2]), _b2i(raw[1])),
            'hardware': _b2i(raw[3]),
            'serialnumber': serialnumber,
        }
        return data

    def get_health(self):
        '''Get device health state. When the core system detects some
        potential risk that may cause hardware failure in the future,
        the returned status value will be 'Warning'. But sensor can still work
        as normal. When sensor is in the Protection Stop state, the returned
        status value will be 'Error'. In case of warning or error statuses
        non-zero error code will be returned.

        Returns
        -------
        status : str
            'Good', 'Warning' or 'Error' statuses
        error_code : int
            The related error code that caused a warning/error.
        '''
        if self._serial.inWaiting() > 0:
            return ('Data in buffer, you can\'t have info ! '
                    'Run clean_input() to emptied the buffer.')
        self.logger.info('Asking for health')
        self._send_cmd(GET_HEALTH_BYTE)
        dsize, is_single, dtype = self._read_descriptor()
        if dsize != HEALTH_LEN:
            raise RPLidarException('Wrong get_info reply length')
        if not is_single:
            raise RPLidarException('Not a single response mode')
        if dtype != HEALTH_TYPE:
            raise RPLidarException('Wrong response data type')
        raw = self._read_response(dsize)
        status = _HEALTH_STATUSES[_b2i(raw[0])]
        error_code = (_b2i(raw[1]) << 8) + _b2i(raw[2])
        return status, error_code

    def clean_input(self):
        '''Clean input buffer by reading all available data'''
        if self.scanning[0]:
            return 'Cleanning not allowed during scanning process active !'
        self._serial.flushInput()
        self.express_trame = 32
        self.express_data = False

    def stop(self):
        '''Stops scanning process, disables laser diode and the measurement
        system, moves sensor to the idle state.'''
        self.logger.info('Stopping scanning')
        self._send_cmd(STOP_BYTE)
        time.sleep(.1)
        self.scanning[0] = False
        self.clean_input()

    def start(self, scan_type='normal'):
        '''Start the scanning process

        Parameters
        ----------
        scan : normal, force or express.
        '''
        if self.scanning[0]:
            return 'Scanning already running !'
        '''Start the scanning process, enable laser diode and the
        measurement system'''
        status, error_code = self.get_health()
        self.logger.debug('Health status: %s [%d]', status, error_code)
        if status == _HEALTH_STATUSES[2]:
            self.logger.warning('Trying to reset sensor due to the error. '
                                'Error code: %d', error_code)
            self.reset()
            status, error_code = self.get_health()
            if status == _HEALTH_STATUSES[2]:
                raise RPLidarException('RPLidar hardware failure. '
                                       'Error code: %d' % error_code)
        elif status == _HEALTH_STATUSES[1]:
            self.logger.warning('Warning sensor status detected! '
                                'Error code: %d', error_code)

        cmd = _SCAN_TYPE[scan_type]['byte']
        self.logger.info('starting scan process in %s mode' % scan_type)

        if scan_type == 'express':
            self._send_payload_cmd(cmd, b'\x00\x00\x00\x00\x00')
        else:
            self._send_cmd(cmd)

        dsize, is_single, dtype = self._read_descriptor()
        if dsize != _SCAN_TYPE[scan_type]['size']:
            raise RPLidarException('Wrong get_info reply length')
        if is_single:
            raise RPLidarException('Not a multiple response mode')
        if dtype != _SCAN_TYPE[scan_type]['response']:
            raise RPLidarException('Wrong response data type')
        self.scanning = [True, dsize, scan_type]

    def reset(self):
        '''Resets sensor core, reverting it to a similar state as it has
        just been powered up.'''
        self.logger.info('Resetting the sensor')
        self._send_cmd(RESET_BYTE)
        time.sleep(2)
        self.clean_input()

    def iter_measures(self, scan_type='normal', max_buf_meas=3000):
        '''Iterate over measures. Note that consumer must be fast enough,
        otherwise data will be accumulated inside buffer and consumer will get
        data with increasing lag.

        Parameters
        ----------
        max_buf_meas : int or False if you want unlimited buffer
            Maximum number of bytes to be stored inside the buffer. Once
            numbe exceeds this limit buffer will be emptied out.

        Yields
        ------
        new_scan : bool
            True if measures belongs to a new scan
        quality : int
            Reflected laser pulse strength
        angle : float
            The measure heading angle in degree unit [0, 360)
        distance : float
            Measured object distance related to the sensor's rotation center.
            In millimeter unit. Set to 0 when measure is invalid.
        '''
        self.start_motor()
        if not self.scanning[0]:
            self.start(scan_type)
        while True:
            dsize = self.scanning[1]
            if max_buf_meas:
                data_in_buf = self._serial.inWaiting()
                if data_in_buf > max_buf_meas:
                    self.logger.warning(
                        'Too many bytes in the input buffer: %d/%d. '
                        'Cleaning buffer...',
                        data_in_buf, max_buf_meas)
                    self.stop()
                    self.start(self.scanning[2])

            if self.scanning[2] == 'normal':
                raw = self._read_response(dsize)
                yield _process_scan(raw)
            if self.scanning[2] == 'express':
                if self.express_trame == 32:
                    self.express_trame = 0
                    if not self.express_data:
                        self.logger.debug('reading first time bytes')
                        self.express_data = ExpressPacket.from_string(
                                            self._read_response(dsize))

                    self.express_old_data = self.express_data
                    self.logger.debug('set old_data with start_angle %f',
                                      self.express_old_data.start_angle)
                    self.express_data = ExpressPacket.from_string(
                                        self._read_response(dsize))
                    self.logger.debug('set new_data with start_angle %f',
                                      self.express_data.start_angle)

                self.express_trame += 1
                self.logger.debug('process scan of frame %d with angle : '
                                  '%f and angle new : %f', self.express_trame,
                                  self.express_old_data.start_angle,
                                  self.express_data.start_angle)
                yield _process_express_scan(self.express_old_data,
                                            self.express_data.start_angle,
                                            self.express_trame)

    def iter_scans(self, scan_type='normal', max_buf_meas=3000, min_len=5):
        '''Iterate over scans. Note that consumer must be fast enough,
        otherwise data will be accumulated inside buffer and consumer will get
        data with increasing lag.

        Parameters
        ----------
        max_buf_meas : int
            Maximum number of measures to be stored inside the buffer. Once
            numbe exceeds this limit buffer will be emptied out.
        min_len : int
            Minimum number of measures in the scan for it to be yelded.

        Yields
        ------
        scan : list
            List of the measures. Each measurment is tuple with following
            format: (quality, angle, distance). For values description please
            refer to `iter_measures` method's documentation.
        '''
        scan_list = []
        iterator = self.iter_measures(scan_type, max_buf_meas)
        for new_scan, quality, angle, distance in iterator:
            if new_scan:
                if len(scan_list) > min_len:
                    yield scan_list
                scan_list = []
            if distance > 0:
                scan_list.append((quality, angle, distance))


class ExpressPacket(namedtuple('express_packet',
                               'distance angle new_scan start_angle')):
    sync1 = 0xa
    sync2 = 0x5
    sign = {0: 1, 1: -1}

    @classmethod
    def from_string(cls, data):
        packet = bytearray(data)

        if (packet[0] >> 4) != cls.sync1 or (packet[1] >> 4) != cls.sync2:
            raise ValueError('try to parse corrupted data ({})'.format(packet))

        checksum = 0
        for b in packet[2:]:
            checksum ^= b
        if checksum != (packet[0] & 0b00001111) + ((
                        packet[1] & 0b00001111) << 4):
            raise ValueError('Invalid checksum ({})'.format(packet))

        new_scan = packet[3] >> 7
        start_angle = (packet[2] + ((packet[3] & 0b01111111) << 8)) / 64

        d = a = ()
        for i in range(0,80,5):
            d += ((packet[i+4] >> 2) + (packet[i+5] << 6),)
            a += (((packet[i+8] & 0b00001111) + ((
                    packet[i+4] & 0b00000001) << 4))/8*cls.sign[(
                     packet[i+4] & 0b00000010) >> 1],)
            d += ((packet[i+6] >> 2) + (packet[i+7] << 6),)
            a += (((packet[i+8] >> 4) + (
                (packet[i+6] & 0b00000001) << 4))/8*cls.sign[(
                    packet[i+6] & 0b00000010) >> 1],)
        return cls(d, a, new_scan, start_angle)


def CA_SlotFront():
    # slot measurements into CA Front    
    if(((measurement[idx_AngleDeg] >350.0) and (measurement[idx_AngleDeg] < 359.99999)) or ((measurement[idx_AngleDeg] >0.0) and (measurement[idx_AngleDeg] < 10.0))):
        # slot measurement into obstacleMap
        if((measurement[idx_DistMm]>200) and (measurement[idx_DistMm]<8000)):
            if(measurement[idx_DistMm]<1000):
                obstacleMap[7][obstacleMap_CenterCol] = measurement[idx_DistMm]                
                if ser is not None:
                    ser.write(b"\x15") # d21, dir (2) forward, zone 1 = 0x15
                #print('Obstacle Front -> Dist: {} mm / QOL ---> {} '.format(measurement[idx_DistMm],measurement[idx_QOL]))
            elif(measurement[idx_DistMm]<2000):
                obstacleMap[6][obstacleMap_CenterCol] = measurement[idx_DistMm] 
                if ser is not None:
                    ser.write(b"\x16")  # d22, dir forward (2), zone 2 = 0x16
                # debug code
                #print('Obstacle Front -> Dist: {} mm / QOL ---> {} '.format(measurement[idx_DistMm],measurement[idx_QOL]))           
                #print("Hello from RC")
            elif(measurement[idx_DistMm]<3000):
                obstacleMap[5][obstacleMap_CenterCol] = measurement[idx_DistMm]
                #if ser is not None:
                    #ser.write(b"\x17")  # d23, dir forward (2), zone 3                              
            elif (measurement[idx_DistMm]<4000):
                obstacleMap[4][obstacleMap_CenterCol] = measurement[idx_DistMm]
                #if ser is not None:
                    #ser.write(b"\x18")  # d24, dir forward (2), zone 4
            elif (measurement[idx_DistMm]<5000):
                obstacleMap[3][obstacleMap_CenterCol] = measurement[idx_DistMm]
                #if ser is not None:
                    #ser.write(b"\x19")  # d25, dir forward (2), zone 5                
            elif (measurement[idx_DistMm]<6000):
                obstacleMap[2][obstacleMap_CenterCol] = measurement[idx_DistMm]
                #if ser is not None:
                    #ser.write(b"\x1A")  # d26, dir forward (2), zone 6                
            elif (measurement[idx_DistMm]<7000):                
                obstacleMap[1][obstacleMap_CenterCol] = measurement[idx_DistMm]
                #if ser is not None:
                    #ser.write(b"\x1B")  # d27, dir forward (2), zone 7
            elif (measurement[idx_DistMm]<8000):                
                obstacleMap[0][obstacleMap_CenterCol] = measurement[idx_DistMm]
                #if ser is not None:
                    #ser.write(b"\x1C")  # d27, dir forward (2), zone 8

def CA_SlotFront_ShowRange():
    # debug code
    for i in range(obstacleMap_CenterRow-1,-1,-1) :
        if(obstacleMap[i][obstacleMap_CenterCol]>0) :
            print('Obstacle at FRONT -> row: {}, col: {} / Dist: -> {} mm'.format(i,obstacleMap_CenterCol, measurement[idx_DistMm]))

def CA_SlotFront_ShowQOL():
    # debug code
    for i in range(obstacleMap_CenterRow-1,-1,-1) :
        if(obstacleMap[i][obstacleMap_CenterCol]>0) :
            print('Obstacle at FRONT ->  Dist: -> {} mm / QOL: -> {} '.format(measurement[idx_DistMm], measurement[idx_QOL]))
            
                    
def CA_SlotBack():
    # slot measurements into CA Back
    if((measurement[idx_AngleDeg] >172.0) and (measurement[idx_AngleDeg] < 188.0)):
        # slot measurement into obstacleMap
        if((measurement[idx_DistMm]>200) and (measurement[idx_DistMm]<8000)):                
            if(measurement[idx_DistMm]<1000):
                obstacleMap[9][obstacleMap_CenterCol] = measurement[idx_DistMm]                    
                if ser is not None:
                    ser.write(b"\x51") # d81, Back Zone 1
            elif(measurement[idx_DistMm]<2000):
                obstacleMap[10][obstacleMap_CenterCol] = measurement[idx_DistMm]
                if ser is not None:
                    ser.write(b"\x52") # d82, Back Zone 2
            elif (measurement[idx_DistMm]<3000):
                obstacleMap[11][obstacleMap_CenterCol] = measurement[idx_DistMm]
                #if ser is not None:
                    #ser.write(b"\x53") # d83, Back Zone 3                                
            elif (measurement[idx_DistMm]<4000):
                obstacleMap[12][obstacleMap_CenterCol] = measurement[idx_DistMm]
                #if ser is not None:
                    #ser.write(b"\x54") # d84, Back Zone 4
            elif (measurement[idx_DistMm]<5000):
                obstacleMap[13][obstacleMap_CenterCol] = measurement[idx_DistMm]
                #if ser is not None:
                    #ser.write(b"\x55") # d85, Back Zone 5
            elif (measurement[idx_DistMm]<6000):                
                obstacleMap[14][obstacleMap_CenterCol] = measurement[idx_DistMm]
                #if ser is not None:
                    #ser.write(b"\x56") # d86, Back Zone 6
            elif (measurement[idx_DistMm]<7000):
                obstacleMap[15][obstacleMap_CenterCol] = measurement[idx_DistMm]
                #if ser is not None:
                    #ser.write(b"\x57") # d87, Back Zone 7
            elif (measurement[idx_DistMm]<8000):
                obstacleMap[16][obstacleMap_CenterCol] = measurement[idx_DistMm]
                #if ser is not None:
                    #ser.write(b"\x58") # d88, Back Zone 8



def CA_SlotBack_ShowRange():
    for i in range(obstacleMap_CenterRow+1,17) :
        if(obstacleMap[i][obstacleMap_CenterCol]>0) :
            print('Obstacle at BACK -> row: {}, col: {} / Dist: -> {} mm'.format(i,obstacleMap_CenterCol, measurement[idx_DistMm]))

def CA_SlotBack_ShowQOL():
    for i in range(obstacleMap_CenterRow+1,17) :
        if(obstacleMap[i][obstacleMap_CenterCol]>0) :
            print('Obstacle at Back ->  Dist: -> {} mm / QOL: -> {} '.format(measurement[idx_DistMm], measurement[idx_QOL]))

def CA_SlotLeft():
    # slot measurements into CA Left
    if((measurement[idx_AngleDeg] >260.0) and (measurement[idx_AngleDeg] < 280.0)) :
        # slot measurement into obstacleMap
        if((measurement[idx_DistMm]>200) and (measurement[idx_DistMm]<8000)):
            if(measurement[idx_DistMm]<1000):                
                obstacleMap[obstacleMap_CenterRow][7] = measurement[idx_DistMm]                    
                if ser is not None:
                    ser.write(b"\x29") #d41, direction 4, Zone1                        
            elif(measurement[idx_DistMm]<2000):                
                obstacleMap[obstacleMap_CenterRow][6] = measurement[idx_DistMm]
                if ser is not None:
                    ser.write(b"\x2A") #d42, direction 4, zone 2
            elif (measurement[idx_DistMm]<3000):                
                obstacleMap[obstacleMap_CenterRow][5] = measurement[idx_DistMm]
                #if ser is not None:
                    #ser.write(b"\x2B") #d43, direction 4, zone 3
            elif (measurement[idx_DistMm]<4000):                
                obstacleMap[obstacleMap_CenterRow][4] = measurement[idx_DistMm]
                #if ser is not None:
                    #ser.write(b"\x2C") #d44, direction 4, zone 4
            elif (measurement[idx_DistMm]<5000):                
                obstacleMap[obstacleMap_CenterRow][3] = measurement[idx_DistMm]
                #if ser is not None:
                    #ser.write(b"\x2D") #d45, direction 4, zone 5
            elif (measurement[idx_DistMm]<6000):                
                obstacleMap[obstacleMap_CenterRow][2] = measurement[idx_DistMm]
                #if ser is not None:
                    #ser.write(b"\x2E") #d46, direction 4, zone 6
            elif (measurement[idx_DistMm]<7000):                
                obstacleMap[obstacleMap_CenterRow][1] = measurement[idx_DistMm]
                #if ser is not None:
                    #ser.write(b"\x2F") #d47, direction 4, zone 7
            elif (measurement[idx_DistMm]<8000):                
                obstacleMap[obstacleMap_CenterRow][0] = measurement[idx_DistMm]
                #if ser is not None:
                    #ser.write(b"\x30") #d48, direction 4, zone 8
                    
def CA_SlotLeft_ShowRange():
    for i in range(0,obstacleMap_CenterCol) :
        if(obstacleMap[obstacleMap_CenterRow][i]>0) :
            print('Obstacle at LEFT -> row: {}, col: {} / Dist: -> {} mm'.format(obstacleMap_CenterRow,i,measurement[idx_DistMm]))

def CA_SlotLeft_ShowQOL():
    for i in range(0,obstacleMap_CenterCol) :
        if(obstacleMap[obstacleMap_CenterRow][i]>0) :
            print('Obstacle at LEFT ->  Dist: -> {} mm / QOL: -> {} '.format(measurement[idx_DistMm], measurement[idx_QOL])) 
            
def CA_SlotRight():
    # slot measurements into CA Right
    if((measurement[idx_AngleDeg] >80.0) and (measurement[idx_AngleDeg] < 100.0)):
        # slot measurement into obstacleMap        
        if((measurement[idx_DistMm]>200) and (measurement[idx_DistMm]<8000)):                
            if(measurement[idx_DistMm]<1000):                
                obstacleMap[obstacleMap_CenterRow][9] = measurement[idx_DistMm]
                if ser is not None:
                    ser.write(b"\x3D") #aka 61 -> dir Right, Zone 1            
            elif(measurement[idx_DistMm]<2000):                
                obstacleMap[obstacleMap_CenterRow][10] = measurement[idx_DistMm]                
                if ser is not None:
                    ser.write(b"\x3E") #aka 62 -> dir Right, Zone 2
            elif(measurement[idx_DistMm]<3000):                
                obstacleMap[obstacleMap_CenterRow][11] = measurement[idx_DistMm]
                #if ser is not None:
                    #ser.write(b"\x3F") #aka 63 -> dir Right, Zone 3
            elif(measurement[idx_DistMm]<4000):                
                obstacleMap[obstacleMap_CenterRow][12] = measurement[idx_DistMm]
                #if ser is not None:
                    #ser.write(b"\x40") #aka 64 -> dir Right, Zone 4
            elif(measurement[idx_DistMm]<5000):                
                obstacleMap[obstacleMap_CenterRow][13] = measurement[idx_DistMm]
                #if ser is not None:
                    #ser.write(b"\x41") #aka 65 -> dir Right, Zone 5
            elif(measurement[idx_DistMm]<6000):                
                obstacleMap[obstacleMap_CenterRow][14] = measurement[idx_DistMm]
                #if ser is not None:
                    #ser.write(b"\x42") #aka 66 -> dir Right, Zone 6
            elif(measurement[idx_DistMm]<7000):                
                obstacleMap[obstacleMap_CenterRow][15] = measurement[idx_DistMm]
                #if ser is not None:
                    #ser.write(b"\x43") #aka 67 -> dir Right, Zone 7
            elif(measurement[idx_DistMm]<8000):                
                obstacleMap[obstacleMap_CenterRow][16] = measurement[idx_DistMm]
                #if ser is not None:
                    #ser.write(b"\x44") #aka 68 -> dir Right, Zone 8

def CA_SlotRight_ShowRange():
    for i in range(obstacleMap_CenterCol,obstacleMap_Col_Len) :
        if(obstacleMap[obstacleMap_CenterRow][i]>0) :                            
            print('Obstacle at RIGHT ->  Dist: -> {} mm / QOL: -> {} '.format(measurement[idx_DistMm], measurement[idx_QOL]))


def CA_SlotRight_ShowQOL():
    for i in range(obstacleMap_CenterCol,obstacleMap_Col_Len) :
        if(obstacleMap[obstacleMap_CenterRow][i]>0) :                            
            print('Obstacle at RIGHT ->  Dist: -> {} mm / QOL: -> {} '.format(measurement[idx_DistMm], measurement[idx_QOL]))



# Main()
# ===========================================================================================================

try:
    ser = serial.Serial(arduinoPort,115200,timeout=0.1)    
    ser.reset_input_buffer()
except:
    ser = None
    print('\nArduino MCU\nunable to access serial port.')
    #sys.exit()

try:
    lidar = RPLidar(lidarPort)
except:
    print('\nRplidarA2M8\nUnable to connect to Lidar port')
    sys.exit()

    
#info = lidar.get_info()
#print('\nRplidar A2M8\n{}'.format(info))

#health = lidar.get_health()
#print('\nRplidar A2M8\n{}'.format(health))
#print('\nStarting Lidar now ...\n')

#print('press enter to continue')
#input()	

##try:
##    print('\nRPlidar A2M8\n...press enter key to continue ...\n...or ctrl-c to stop...')
##    input()
##except KeyboardInterrupt:
##    print('\n ...Exiting now...\n')
##    sys.exit()


obstacleMap = np.zeros((obstacleMap_Row_Len,obstacleMap_Col_Len),int)

##for i, scan in enumerate(lidar.iter_scans()):
##    print('%d: Got %d measures' % (i, len(scan)))
##    if i > 10:
##        break

##try:
##    for measure in lidar.iter_measures(max_buf_meas=500):
##        print('\n {}'.format(measure))
##        #print(measure)
##except KeyboardInterrupt:
##        print('\n... Stopping ...\n')

read_serial = ''
MCUInput = ''

lidarTimer_Treshold = 0.025 # 0.05 second
lidarTimer_Prev = time.time()
    
for measurement in lidar.iter_measures(max_buf_meas=500):    
    # ~~~~~~~~ chk FRONT start ~~~~~~~~~~~~~~~~~~~~
    # Lidar only checks and sends the results to MCU. It does NOT make any kind of
    # decision of whether to stop the robot or not.
    # each cycle, each 'measurement' is checked for the zone.
    # if it falls within any of the 8 zones, this module will send the data to MCU
    # it is up to MCU to decide what to do with the data or to ignore it.
    # in future, each of the 8 zones will send data to MCU and will send the 'row/col' coordinates
    # so that MCU can fill in the entire MAP array
    # for now, in the interest of time, we only send zone 1 and 2 (<2m>1m, and <1m)
    # and instead of 'row/col' coord, we send in terms of 'dir (1 to 9) / Zone#'        

    lidarTimer_Now = time.time()
    if((lidarTimer_Now - lidarTimer_Prev) > lidarTimer_Treshold):
        CA_SlotFront()        
        CA_SlotFront_ShowRange()
        #CA_SlotFront_ShowQOL()
        
        CA_SlotLeft()        
        #CA_SlotLeft_ShowRange()
        #CA_SlotLeft_ShowQOL()
        
        CA_SlotRight()
        #CA_SlotRight_ShowRange()
        #CA_SlotRight_ShowQOL()
        
        CA_SlotBack()
        #CA_SlotBack_ShowRange()
        #CA_SlotBack_ShowQOL()                
        
        # ~~~~~~~~ RESET obstacle map ~~~~~~~~~~~~~~~~~~~~
        obstacleMap.fill(0)        
        lidarTimer_Prev = lidarTimer_Now



