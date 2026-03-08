# DGUS DWIN screen protocol parser
#
# Copyright (C) 2023  E4ST2W3ST
# Copyright (C) 2026  Jonas Kennedy
#
# This file may be distributed under the terms of the GNU GPLv3 license.

SERIAL_STATE_HEADER_NONE = 0
SERIAL_STATE_HEADER_ONE = 1
SERIAL_STATE_HEADER_TWO = 2
SERIAL_STATE_HEADER_MESSAGE = 3

SERIAL_HEADER_BYTE_1 = 0x5a
SERIAL_HEADER_BYTE_2 = 0xa5

DGUS_CMD_WRITEVAR = 0x82
DGUS_CMD_READVAR = 0x83


class Message:
    def __init__(self):
        self.command = None
        self.payload = []
        self.length = None
        self.command_data_length = None
        self.command_data = None
        self.command_address = None

    def process_datagram(self):
        self.command = self.payload[0]
        self.command_address = (
            (self.payload[1] & 0xff) << 8) | (self.payload[2] & 0xff)
        self.command_data_length = self.payload[3]

        self.command_data = []
        it = iter(self.payload[4:])
        for byte in it:
            self.command_data.append(((byte & 0xff) << 8) | (next(it) & 0xff))

    def __str__(self):
        payload_str = ' '.join(['0x%02x' % byte for byte in self.payload])
        return ('payload: %s, '
                'length: %s, command: 0x%02x, '
                'command_address: 0x%04x '
                'command_data_length: %s, '
                'command_data: %s' % (
                    payload_str,
                    self.length,
                    self.command,
                    self.command_address,
                    self.command_data_length,
                    self.command_data
                ))


class DGUSParser:
    """State machine that parses incoming bytes into DGUS Message objects."""
    def __init__(self):
        self._state = SERIAL_STATE_HEADER_NONE
        self._current_message = None

    def parse(self, data):
        """Feed raw bytes, return list of complete Message objects."""
        messages = []
        for byte in data:
            if self._state == SERIAL_STATE_HEADER_NONE:
                if byte == SERIAL_HEADER_BYTE_1:
                    self._state = SERIAL_STATE_HEADER_ONE
                else:
                    self._state = SERIAL_STATE_HEADER_NONE
            elif self._state == SERIAL_STATE_HEADER_ONE:
                if byte == SERIAL_HEADER_BYTE_2:
                    self._state = SERIAL_STATE_HEADER_TWO
                else:
                    self._state = SERIAL_STATE_HEADER_NONE
            elif self._state == SERIAL_STATE_HEADER_TWO:
                self._state = SERIAL_STATE_HEADER_MESSAGE
                self._current_message = Message()
                self._current_message.payload = []
                self._current_message.length = byte
            elif self._state == SERIAL_STATE_HEADER_MESSAGE:
                self._current_message.payload.append(byte)
                if len(self._current_message.payload) == \
                        self._current_message.length:
                    messages.append(self._current_message)
                    self._current_message = None
                    self._state = SERIAL_STATE_HEADER_NONE
        return messages
