#!/usr/bin/env python3
# Standalone Neptune 3 DGUS DWIN screen daemon
#
# Communicates with Klipper via the Unix socket API and drives the
# touchscreen through the serial_bridge webhooks endpoints.
#
# Copyright (C) 2023  E4ST2W3ST
# Copyright (C) 2026  Jonas Kennedy
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import argparse
import configparser
import logging
import os
import signal
import sys
import threading
import time
from abc import ABCMeta, abstractmethod

from klipper_client import KlipperClient
from dgus_protocol import (
    DGUSParser, DGUS_CMD_READVAR,
)

log = logging.getLogger("neptune_screen")

# ── DGUS address constants ──────────────────────────────────────────
DGUS_KEY_MAIN_PAGE          = 0x1002
DGUS_KEY_STOP_PRINT         = 0x1008
DGUS_KEY_PAUSE_PRINT        = 0x100A
DGUS_KEY_RESUME_PRINT       = 0x100C
DGUS_KEY_ADJUSTMENT         = 0x1004
DGUS_KEY_TEMP_SCREEN        = 0x1030
DGUS_KEY_SETTING_BACK_KEY   = 0x1040
DGUS_KEY_COOL_SCREEN        = 0x1032
DGUS_KEY_HEATER0_TEMP_ENTER = 0x1034
DGUS_KEY_HOTBED_TEMP_ENTER  = 0x103A
DGUS_KEY_SETTING_SCREEN     = 0x103E
DGUS_KEY_BED_LEVEL          = 0x1044
DGUS_KEY_AXIS_PAGE_SELECT   = 0x1046
DGUS_KEY_XAXIS_MOVE_KEY     = 0x1048
DGUS_KEY_YAXIS_MOVE_KEY     = 0x104A
DGUS_KEY_ZAXIS_MOVE_KEY     = 0x104C
DGUS_KEY_HEATER0_LOAD_ENTER = 0x1054
DGUS_KEY_FILAMENT_LOAD      = 0x1056
DGUS_KEY_HEATER1_LOAD_ENTER = 0x1058
DGUS_KEY_POWER_CONTINUE     = 0x105F
DGUS_KEY_PRINT_FILE         = 0x2198
DGUS_KEY_SELECT_FILE        = 0x2199
DGUS_KEY_HARDWARE_TEST      = 0x2202
DGUS_KEY_PRINT_FILES        = 0x2204
DGUS_KEY_PRINT_CONFIRM      = 0x2205


# ── Screen wrapper ──────────────────────────────────────────────────

class NeptuneScreen:
    """Manages all screen state and communication with Klipper."""

    def __init__(self, client, bridge_name, variant="3Pro",
                 led_name="my_led", heaters=None, gcodes_dir=None,
                 update_interval=2):
        self.client = client
        self.bridge_name = bridge_name
        self.variant = variant
        self.led_name = led_name
        self.heater_names = heaters or ["extruder", "heater_bed"]
        self.gcodes_dir = gcodes_dir
        self._update_interval = update_interval
        self._parser = DGUSParser()
        self._axis_unit = 1
        self._temp_and_rate_unit = 1
        self._filament_load_length = 50
        self._filament_load_feedrate = 150
        self._acceleration_unit = 100
        self._speed_ctrl = 'feedrate'
        self._temp_ctrl = 'extruder'
        self._print_state = None
        self._zoffset_unit = 0.1
        self._file_list = []
        self._requested_file = None
        self._file_page_number = 0
        self._file_per_page = 8
        self._version = 100
        self._last_gcode_output = ""
        self._stop = threading.Event()
        self._gcode_lock = threading.Lock()
        self._status_cache = {}

    # ── connection helpers ───────────────────────────────────────────

    def start(self):
        """Connect to Klipper, subscribe to events, start update loop."""
        self.client.connect()

        # Subscribe to serial bridge data
        self.client.serial_bridge_subscribe(
            self.bridge_name, "serial_bridge_data")
        self.client.register_subscription(
            "serial_bridge_data", self._on_bridge_data)

        # Subscribe to printer objects we poll
        sub_objects = {
            "print_stats": None,
            "gcode_move": None,
            "fan": None,
            "toolhead": None,
            "virtual_sdcard": None,
            "configfile": None,
        }
        for h in self.heater_names:
            sub_objects[h] = None
        result = self.client.subscribe_objects(sub_objects)
        # Seed status cache with initial values
        initial = result.get("status", {})
        for key, val in initial.items():
            if isinstance(val, dict):
                self._status_cache.setdefault(key, {}).update(val)
            else:
                self._status_cache[key] = val
        self.client.register_subscription(
            "notify_status_update", self._on_status_update)

        # Register gcode response handler
        self.client.register_subscription(
            "notify_gcode_response", self._on_gcode_response)

        # Initial reset
        self._reset_screen()

        # Polling loop
        while not self._stop.is_set():
            try:
                self._screen_update()
            except Exception:
                log.exception("Error in screen update loop")
            self._stop.wait(self._update_interval)

    def stop(self):
        self._stop.set()
        self.client.disconnect()

    # ── Klipper queries ──────────────────────────────────────────────

    def _query(self, *objects):
        """Query printer objects and return the result dict."""
        obj_dict = {o: None for o in objects}
        result = self.client.query_objects(obj_dict)
        return result.get("status", {})

    def _run_gcode(self, script, callback=None):
        """Run gcode, optionally call callback on success."""
        def _work():
            with self._gcode_lock:
                log.debug("Running gcode: %s", script)
                try:
                    self.client.run_gcode(script)
                    if callback:
                        callback()
                except Exception as e:
                    log.error("Gcode error: %s", e)
                    self.send_text("page wait")
                    self.update_text("wait.t1.txt",
                                     str(self._last_gcode_output))
                    self.send_text("beep 2000")
                    time.sleep(4)
                    self.send_text("page main")
        threading.Thread(target=_work, daemon=True).start()

    # ── Status cache ─────────────────────────────────────────────────

    def _on_status_update(self, params):
        """Merge incremental status updates into cache."""
        status = params.get("status") if isinstance(params, dict) else None
        if not status:
            # params may itself be a list [status, eventtime]
            if isinstance(params, list) and len(params) >= 1:
                status = params[0]
        if isinstance(status, dict):
            for key, val in status.items():
                if key not in self._status_cache:
                    self._status_cache[key] = {}
                if isinstance(val, dict):
                    self._status_cache[key].update(val)
                else:
                    self._status_cache[key] = val

    def _on_gcode_response(self, params):
        if isinstance(params, list) and params:
            self._last_gcode_output = params[0]
        elif isinstance(params, str):
            self._last_gcode_output = params

    def _get_status(self, *objects):
        """Return cached status for requested objects, or query if missing."""
        result = {}
        missing = []
        for obj in objects:
            if obj in self._status_cache:
                result[obj] = self._status_cache[obj]
            else:
                missing.append(obj)
        if missing:
            queried = self._query(*missing)
            for obj in missing:
                val = queried.get(obj, {})
                self._status_cache[obj] = val
                result[obj] = val
        return result

    # ── Screen communication ─────────────────────────────────────────

    def send_text(self, text):
        """Send a DGUS text command through the serial bridge."""
        try:
            self.client.serial_bridge_send(self.bridge_name, text)
        except Exception:
            log.exception("Failed to send text: %s", text)

    def update_text(self, key, value):
        self.send_text('%s="%s"' % (key, value))

    def update_numeric(self, key, value):
        self.send_text("%s=%s" % (key, value))

    # ── Serial bridge data handler ───────────────────────────────────

    def _on_bridge_data(self, params):
        data = params.get("data", [])
        if not data:
            return
        byte_debug = ' '.join(['0x%02x' % b for b in data])
        log.debug("Received: %s", byte_debug)

        messages = self._parser.parse(data)
        for msg in messages:
            msg.process_datagram()
            self._process_message(msg)

    def _process_message(self, message):
        log.debug("Process message: %s", message)
        if message.command == DGUS_CMD_READVAR:
            for processor in COMMAND_PROCESSORS:
                processor.process_if_match(message, self)

    # ── Variant helper ───────────────────────────────────────────────

    def _get_variant(self):
        if self.variant == "3Pro":
            return 1
        elif self.variant == "3Max":
            return 3
        elif self.variant == "3Plus":
            return 2
        return 1

    # ── Print time estimate ──────────────────────────────────────────

    def get_estimated_print_time(self):
        s = self._get_status("print_stats", "virtual_sdcard")
        stats = s.get("print_stats", {})
        sd = s.get("virtual_sdcard", {})
        duration = stats.get("print_duration", 0)
        progress = sd.get("progress", 0)
        if progress > 0:
            return duration / progress
        return duration

    # ── File list ────────────────────────────────────────────────────

    def update_file_list(self):
        try:
            self.client.run_gcode(
                "SDCARD_PRINT_FILE_LIST", timeout=5.0)
        except Exception:
            pass
        # Use gcode response to list files, or scan gcodes dir
        try:
            if self.gcodes_dir:
                gcodes_dir = os.path.expanduser(self.gcodes_dir)
            else:
                gcodes_dir = os.path.expanduser("~/printer_data/gcodes")
                if not os.path.isdir(gcodes_dir):
                    gcodes_dir = os.path.expanduser("~/gcode_files")
            if os.path.isdir(gcodes_dir):
                files = []
                for f in sorted(os.listdir(gcodes_dir)):
                    fpath = os.path.join(gcodes_dir, f)
                    if os.path.isfile(fpath) and f.lower().endswith(
                            ('.gcode', '.g', '.gco')):
                        files.append((f, os.path.getsize(fpath)))
                self._file_list = files
            else:
                self._file_list = []
        except Exception:
            log.exception("Failed to get file list")
            self._file_list = []

        current_idx = self._file_page_number * self._file_per_page
        next_idx = current_idx + self._file_per_page

        for i in range(8):
            self.update_text("printfiles.t%d.txt" % i, "")

        index = 0
        for fname, fsize in self._file_list:
            if current_idx <= index < next_idx:
                log.debug("Sending file %s", fname)
                self.update_text(
                    "printfiles.t%d.txt" % (index % self._file_per_page),
                    fname)
            index += 1

    # ── LED helpers ──────────────────────────────────────────────────

    def _is_led_on(self):
        try:
            s = self._query("led")
            led = s.get("led", {})
            color_data = led.get("color_data", [])
            if color_data and len(color_data[0]) > 3:
                return color_data[0][3] > 0
        except Exception:
            pass
        return False

    # ── Screen init / reset ──────────────────────────────────────────

    def _reset_screen(self):
        log.info("Resetting screen")
        self.send_text("com_star")
        self.send_text("rest")
        time.sleep(2.0)
        self._screen_init()

    def _screen_init(self):
        s = self._get_status("gcode_move", "probe")
        move = s.get("gcode_move", {})
        probe = s.get("probe", {})

        self.send_text("page boot")
        self.send_text("com_star")
        self.send_text("main.va0.val=%d" % self._get_variant())
        self.send_text("page main")
        self.send_text('information.sversion.txt="Klipper"')
        self.update_numeric("restFlag1", "1")
        self.update_numeric("restFlag2", "1")

        # z-offset display
        probe_z = 0
        offsets = probe.get("offsets", [0, 0, 0])
        if len(offsets) > 2:
            probe_z = offsets[2]
        homing_z = move.get("homing_origin", {})
        if isinstance(homing_z, dict):
            homing_z = homing_z.get("z", 0)
        elif isinstance(homing_z, list) and len(homing_z) > 2:
            homing_z = homing_z[2]
        else:
            homing_z = 0
        self.update_numeric(
            "leveldata.z_offset.val", "%.0f" % ((homing_z - probe_z) * 100))

    # ── Periodic update ──────────────────────────────────────────────

    def _screen_update(self):
        query_objs = ["print_stats", "gcode_move", "fan"] + self.heater_names
        s = self._get_status(*query_objs)
        stats = s.get("print_stats", {})
        move = s.get("gcode_move", {})
        fan = s.get("fan", {})

        # Temperatures
        for heater_name in self.heater_names:
            h = s.get(heater_name, {})
            temp_str = "%.0f / %.0f" % (
                h.get("temperature", 0), h.get("target", 0))
            if heater_name == "heater_bed":
                self.update_text("main.bedtemp.txt", temp_str)
            else:
                self.update_text("main.nozzletemp.txt", temp_str)

        # LED status
        if self._is_led_on():
            self.send_text("status_led2=1")
        else:
            self.send_text("status_led2=0")

        # Z position
        pos = move.get("gcode_position", [0, 0, 0, 0])
        z = pos[2] if isinstance(pos, list) and len(pos) > 2 else 0
        self.update_numeric("printpause.zvalue.vvs1", "2")
        self.send_text("printpause.zvalue.val=%.0f" % (z * 100))

        # Fan speed
        fan_speed = fan.get("speed", 0)
        self.update_text("printpause.fanspeed.txt",
                         "%.0f%%" % (fan_speed * 100))

        # Print state transitions
        last_state = self._print_state
        self._print_state = stats.get("state")

        if self._print_state == "printing" and last_state != self._print_state:
            self.send_text("page printpause")
        if self._print_state == "complete" and last_state != self._print_state:
            self.send_text("page main")


# ── Command processors ──────────────────────────────────────────────

class CommandProcessor:
    __metaclass__ = ABCMeta

    def __init__(self, address, command=None):
        self.address = address
        self.command = command

    def is_match(self, message):
        return message.command_address == self.address and (
            self.command is None or self.command == message.command_data[0])

    def process_if_match(self, message, screen):
        if self.is_match(message):
            self.process(message, screen)

    @abstractmethod
    def process(self, data, screen):
        pass


class MainPageProcessor(CommandProcessor):
    def process(self, message, screen):
        if message.command_data[0] == 0x1:
            s = screen._get_status("print_stats")
            stats = s.get("print_stats", {})
            state = stats.get("state", "")

            if state in ['printing', 'paused']:
                screen.send_text("page printpause")
            else:
                screen._file_page_number = 0
                if screen._version >= 142:
                    screen.send_text("page printfiles")
                    screen.update_file_list()
                else:
                    screen.send_text("page file1")
                    screen.update_file_list()


class BedLevelProcessor(CommandProcessor):
    def process(self, message, screen):
        cmd = message.command_data[0]

        if cmd in (0x2, 0x3):
            s = screen._get_status("gcode_move", "probe")
            move = s.get("gcode_move", {})
            probe = s.get("probe", {})

            unit = screen._zoffset_unit
            if cmd == 0x3:
                unit *= -1

            offsets = probe.get("offsets", [0, 0, 0])
            probe_z = offsets[2] if len(offsets) > 2 else 0

            homing = move.get("homing_origin", [0, 0, 0])
            homing_z = homing[2] if isinstance(homing, list) and len(homing) > 2 else 0
            new_offset = homing_z + unit

            screen._run_gcode(
                "SET_GCODE_OFFSET Z=%.3f MOVE=1" % new_offset)
            screen.update_numeric(
                "leveldata.z_offset.val",
                "%.0f" % ((new_offset - probe_z) * 100))
            screen.update_numeric(
                "adjustzoffset.z_offset.val",
                "%.0f" % ((new_offset - probe_z) * 100))

        if cmd == 0x4:
            screen._zoffset_unit = 0.01
            screen.update_numeric("adjustzoffset.zoffset_value.val", "1")
        if cmd == 0x5:
            screen._zoffset_unit = 0.1
            screen.update_numeric("adjustzoffset.zoffset_value.val", "2")
        if cmd == 0x6:
            screen._zoffset_unit = 1
            screen.update_numeric("adjustzoffset.zoffset_value.val", "3")

        if cmd == 0x8:
            # Toggle LED
            if screen._is_led_on():
                screen._run_gcode(
                    "SET_LED LED=%s WHITE=0" % screen.led_name)
            else:
                screen._run_gcode(
                    "SET_LED LED=%s WHITE=1" % screen.led_name)

        if cmd == 0x9:
            screen._run_gcode(
                "BED_MESH_CLEAR\n"
                "M140 S60\nM104 S140\n"
                "M109 S140\nM190 S60\n"
                "BED_MESH_CALIBRATE LIFT_SPEED=2\n"
                "G28\nG1 F200 Z0",
                lambda: (
                    screen.send_text("page leveldata_36"),
                    screen.send_text("page warn_zoffset"),
                ))

        if cmd == 0xa:
            s = screen._get_status("print_stats", "virtual_sdcard", "fan")
            stats = s.get("print_stats", {})
            sd = s.get("virtual_sdcard", {})
            fan = s.get("fan", {})

            estimated = screen.get_estimated_print_time()
            progress = sd.get("progress", 0)

            screen.update_numeric(
                "printpause.printprocess.val", "%.0f" % (progress * 100))
            screen.update_text(
                "printpause.printvalue.txt", "%.0f" % (progress * 100))
            screen.update_text("printpause.t0.txt", stats.get("filename", ""))

            current_min = stats.get("print_duration", 0) / 60.0
            estimated_min = estimated / 60
            screen.update_text("printpause.printtime.txt",
                               "%.0f / %.0f min" % (current_min, estimated_min))
            screen.update_text("printpause.fanspeed.txt",
                               "%.0f%%" % (fan.get("speed", 0) * 100))

        if cmd == 0x16:
            s = screen._get_status("print_stats", "virtual_sdcard",
                                   "gcode_move")
            stats = s.get("print_stats", {})
            sd = s.get("virtual_sdcard", {})
            move = s.get("gcode_move", {})

            estimated = screen.get_estimated_print_time()
            speed_factor = move.get("speed_factor", 1)
            screen.update_text("printpause.printspeed.txt",
                               "%.0f" % (speed_factor * 100))

            current_min = stats.get("print_duration", 0) / 60.0
            estimated_min = estimated / 60
            screen.update_text("printpause.printtime.txt",
                               "%.0f / %.0f min" % (current_min, estimated_min))

            progress = sd.get("progress", 0)
            screen.update_numeric(
                "printpause.printprocess.val", "%.0f" % (progress * 100))
            screen.update_text(
                "printpause.printvalue.txt", "%.0f" % (progress * 100))

            if stats.get("state") == "printing":
                screen.update_numeric("restFlag1", "0")
            else:
                screen.update_numeric("restFlag1", "1")


class AdjustmentProcessor(CommandProcessor):
    def process(self, message, screen):
        cmd = message.command_data[0]

        if cmd == 0x1:
            screen._temp_and_rate_unit = 10
            screen._temp_ctrl = 'extruder'
            s = screen._get_status("extruder")
            target = s.get("extruder", {}).get("target", 0)
            screen.update_numeric("adjusttemp.targettemp.val",
                                  "%.0f" % target)
        if cmd == 0x02:
            screen.send_text("page printpause")
        if cmd == 0x05:
            screen._temp_and_rate_unit = 10
            screen.send_text("page adjusttemp")
        if cmd == 0x06:
            screen._temp_and_rate_unit = 10
            screen._speed_ctrl = 'feedrate'
            s = screen._get_status("gcode_move")
            speed_factor = s.get("gcode_move", {}).get("speed_factor", 1)
            screen.update_numeric("adjustspeed.targetspeed.val",
                                  "%.0f" % (speed_factor * 100))
            screen.send_text("page adjustspeed")
        if cmd == 0x07:
            s = screen._get_status("gcode_move", "probe")
            move = s.get("gcode_move", {})
            probe = s.get("probe", {})

            screen._zoffset_unit = 0.1
            offsets = probe.get("offsets", [0, 0, 0])
            probe_z = offsets[2] if len(offsets) > 2 else 0
            homing = move.get("homing_origin", [0, 0, 0])
            homing_z = homing[2] if isinstance(homing, list) and len(homing) > 2 else 0

            screen.update_numeric("adjustzoffset.zoffset_value.val", "2")
            screen.update_numeric("adjustzoffset.z_offset.val",
                                  "%.0f" % ((homing_z - probe_z) * 100))
            screen.send_text("page adjustzoffset")

        if cmd == 0x08:
            screen._run_gcode("M220 S100")
            screen.update_numeric("adjustspeed.targetspeed.val", "100")
        if cmd == 0x09:
            screen._run_gcode("M221 S100")
            screen.update_numeric("adjustspeed.targetspeed.val", "100")
        if cmd == 0x0A:
            screen._run_gcode("M106 S255")
            screen.update_numeric("adjustspeed.targetspeed.val", "100")


class TempScreenProcessor(CommandProcessor):
    def process(self, message, screen):
        cmd = message.command_data[0]

        if cmd == 0x1:
            screen._temp_ctrl = 'extruder'
            s = screen._get_status("extruder")
            target = s.get("extruder", {}).get("target", 0)
            screen.update_numeric("adjusttemp.targettemp.val",
                                  "%.0f" % target)
        if cmd == 0x3:
            screen._temp_ctrl = 'heater_bed'
            s = screen._get_status("heater_bed")
            target = s.get("heater_bed", {}).get("target", 0)
            screen.update_numeric("adjusttemp.targettemp.val",
                                  "%.0f" % target)
        if cmd == 0x5:
            screen._axis_unit = 0.1
            screen._temp_and_rate_unit = 1
            screen._acceleration_unit = 10
        if cmd == 0x6:
            screen._axis_unit = 1.0
            screen._temp_and_rate_unit = 5
            screen._acceleration_unit = 50
        if cmd == 0x7:
            screen._axis_unit = 10.0
            screen._temp_and_rate_unit = 10
            screen._acceleration_unit = 100

        if cmd in (0x8, 0x9):
            heater_obj = screen._temp_ctrl
            s = screen._get_status(heater_obj)
            target_temp = s.get(heater_obj, {}).get("target", 0)

            max_temp = 230 if screen._temp_ctrl == 'extruder' else 125
            min_temp = 25
            direction = 1 if cmd == 0x8 else -1
            new_target = target_temp + screen._temp_and_rate_unit * direction

            if min_temp <= new_target <= max_temp:
                gcode = 'M104' if screen._temp_ctrl == 'extruder' else 'M140'
                screen._run_gcode("%s S%.0f" % (gcode, new_target))
                screen.update_numeric("adjusttemp.targettemp.val",
                                      "%.0f" % new_target)

        if cmd == 0xA:
            screen._speed_ctrl = 'feedrate'
            s = screen._get_status("gcode_move")
            sf = s.get("gcode_move", {}).get("speed_factor", 1)
            screen.update_numeric("adjustspeed.targetspeed.val",
                                  "%.0f" % (sf * 100))
        if cmd == 0xB:
            screen._speed_ctrl = 'flowrate'
            s = screen._get_status("gcode_move")
            ef = s.get("gcode_move", {}).get("extrude_factor", 1)
            screen.update_numeric("adjustspeed.targetspeed.val",
                                  "%.0f" % (ef * 100))
        if cmd == 0xC:
            screen._speed_ctrl = 'fanspeed'
            s = screen._get_status("fan")
            spd = s.get("fan", {}).get("speed", 0)
            screen.update_numeric("adjustspeed.targetspeed.val",
                                  "%.0f" % (spd * 100))

        if cmd in (0xD, 0xE):
            unit = screen._temp_and_rate_unit
            if cmd == 0xE:
                unit *= -1

            if screen._speed_ctrl == 'feedrate':
                s = screen._get_status("gcode_move")
                sf = s.get("gcode_move", {}).get("speed_factor", 1)
                new_rate = (sf + (unit / 100.0)) * 100
                new_rate = max(new_rate, 0)
                screen._run_gcode("M220 S%.0f" % new_rate)
                screen.update_numeric("adjustspeed.targetspeed.val",
                                      "%.0f" % new_rate)
            elif screen._speed_ctrl == 'flowrate':
                s = screen._get_status("gcode_move")
                ef = s.get("gcode_move", {}).get("extrude_factor", 1)
                new_rate = (ef + (unit / 100.0)) * 100
                new_rate = max(0, min(150, new_rate))
                screen._run_gcode("M221 S%.0f" % new_rate)
                screen.update_numeric("adjustspeed.targetspeed.val",
                                      "%.0f" % new_rate)
            elif screen._speed_ctrl == 'fanspeed':
                s = screen._get_status("fan")
                spd = s.get("fan", {}).get("speed", 0)
                new_rate = spd + (unit / 100.0)
                new_rate = max(0, min(1, new_rate))
                screen._run_gcode("M106 S%.0f" % (new_rate * 255.0))
                screen.update_numeric("adjustspeed.targetspeed.val",
                                      "%.0f" % (new_rate * 100))

        if cmd in (0x10, 0x0F):
            screen._acceleration_unit = 100
            s = screen._get_status("toolhead")
            th = s.get("toolhead", {})
            screen.update_text("speedsetvalue.t0.txt", "Accel.")
            screen.update_text("speedsetvalue.t1.txt",
                               "Max Accel. to Decel.")
            screen.update_text("speedsetvalue.t2.txt", "SCV")
            screen.update_text("speedsetvalue.t3.txt", "Velocity")
            screen.update_numeric("speedsetvalue.xaxis.val",
                                  "%.0f" % th.get("max_accel", 0))
            screen.update_numeric("speedsetvalue.yaxis.val",
                                  "%.0f" % th.get("max_accel_to_decel", 0))
            screen.update_numeric("speedsetvalue.zaxis.val",
                                  "%.0f" % th.get("square_corner_velocity", 0))
            screen.update_numeric("speedsetvalue.eaxis.val",
                                  "%.0f" % th.get("max_velocity", 0))

        if cmd in (0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x18):
            s = screen._get_status("toolhead")
            th = s.get("toolhead", {})
            unit = screen._acceleration_unit

            accel_cmds = {
                0x11: ("max_accel", -1, "ACCEL", "xaxis"),
                0x15: ("max_accel", 1, "ACCEL", "xaxis"),
                0x12: ("max_accel_to_decel", -1, "ACCEL_TO_DECEL", "yaxis"),
                0x16: ("max_accel_to_decel", 1, "ACCEL_TO_DECEL", "yaxis"),
                0x13: ("square_corner_velocity", -1,
                       "SQUARE_CORNER_VELOCITY", "zaxis"),
                0x17: ("square_corner_velocity", 1,
                       "SQUARE_CORNER_VELOCITY", "zaxis"),
                0x14: ("max_velocity", -1, "VELOCITY", "eaxis"),
                0x18: ("max_velocity", 1, "VELOCITY", "eaxis"),
            }
            if cmd in accel_cmds:
                field, direction, param, axis = accel_cmds[cmd]
                u = unit
                if field == "square_corner_velocity":
                    u = unit // 10
                val = th.get(field, 0) + u * direction
                screen._run_gcode(
                    "SET_VELOCITY_LIMIT %s=%.0f" % (param, val))
                screen.update_numeric(
                    "speedsetvalue.%s.val" % axis, "%.0f" % val)


class CoolScreenProcessor(CommandProcessor):
    def process(self, message, screen):
        cmd = message.command_data[0]
        if cmd == 1:
            screen._run_gcode("M104 S0")
        if cmd == 2:
            screen._run_gcode("M140 S0")
        if cmd in (13, 14, 15, 16, 17):
            screen._temp_and_rate_unit = 10


class AxisPageSelectProcessor(CommandProcessor):
    def process(self, message, screen):
        cmd = message.command_data[0]
        if cmd == 1:
            screen._axis_unit = 0.1
        elif cmd == 2:
            screen._axis_unit = 1.0
        elif cmd == 3:
            screen._axis_unit = 10
        elif cmd == 4:
            screen.send_text("page autohome")
            screen._run_gcode("G28", lambda: screen.send_text("page premove"))
        elif cmd == 5:
            screen.send_text("page autohome")
            screen._run_gcode("G28 X",
                              lambda: screen.send_text("page premove"))
        elif cmd == 6:
            screen.send_text("page autohome")
            screen._run_gcode("G28 Y",
                              lambda: screen.send_text("page premove"))
        elif cmd == 7:
            screen.send_text("page autohome")
            screen._run_gcode("G28 Z",
                              lambda: screen.send_text("page premove"))


class XAxisMoveKeyProcessor(CommandProcessor):
    def process(self, message, screen):
        s = screen._get_status("gcode_move")
        pos = s.get("gcode_move", {}).get("gcode_position", [0, 0, 0, 0])
        current = pos[0] if isinstance(pos, list) and len(pos) > 0 else 0
        if message.command_data[0] == 0x01:
            screen._run_gcode("G0 X%.1f" % (current + screen._axis_unit))
        else:
            screen._run_gcode("G0 X%.1f" % (current - screen._axis_unit))


class YAxisMoveKeyProcessor(CommandProcessor):
    def process(self, message, screen):
        s = screen._get_status("gcode_move")
        pos = s.get("gcode_move", {}).get("gcode_position", [0, 0, 0, 0])
        current = pos[1] if isinstance(pos, list) and len(pos) > 1 else 0
        if message.command_data[0] == 0x01:
            screen._run_gcode("G0 Y%.1f" % (current + screen._axis_unit))
        else:
            screen._run_gcode("G0 Y%.1f" % (current - screen._axis_unit))


class ZAxisMoveKeyProcessor(CommandProcessor):
    def process(self, message, screen):
        s = screen._get_status("gcode_move")
        pos = s.get("gcode_move", {}).get("gcode_position", [0, 0, 0, 0])
        current = pos[2] if isinstance(pos, list) and len(pos) > 2 else 0
        if message.command_data[0] == 0x01:
            screen._run_gcode("G0 Z%.1f" % (current + screen._axis_unit))
        else:
            screen._run_gcode("G0 Z%.1f" % (current - screen._axis_unit))


class Heater0KeyProcessor(CommandProcessor):
    def process(self, message, screen):
        temp = ((message.command_data[0] & 0xff00) >> 8) | \
               ((message.command_data[0] & 0x00ff) << 8)
        screen._run_gcode("M104 S%d" % temp)
        screen.send_text('pretemp.nozzletemp.txt=" %d / %d"' % (0, temp))


class HeaterBedKeyProcessor(CommandProcessor):
    def process(self, message, screen):
        temp = ((message.command_data[0] & 0xff00) >> 8) | \
               ((message.command_data[0] & 0x00ff) << 8)
        screen._run_gcode("M140 S%d" % temp)
        screen.send_text('pretemp.bedtemp.txt=" %d / %d"' % (0, temp))


class SettingScreenProcessor(CommandProcessor):
    def process(self, message, screen):
        cmd = message.command_data[0]
        if cmd == 0x1:
            screen.send_text("page autohome")
            screen._run_gcode("G28\ng1 f200 Z0.00", lambda: (
                screen.update_numeric(
                    "leveling.va1.val", "%d" % screen._get_variant()),
                screen.send_text("page leveldata_36"),
                screen.send_text("leveling_36.tm0.en=0"),
                screen.send_text("leveling.tm0.en=0"),
            ))
        if cmd == 0x6:
            screen._run_gcode("M84")
        if cmd == 0x7:
            s = screen._get_status("fan")
            fan_speed = s.get("fan", {}).get("speed", 0)
            if fan_speed:
                screen._run_gcode("M106 S0")
                screen.update_numeric("set.va0.val", "0")
            else:
                screen._run_gcode("M106 S255")
                screen.update_numeric("set.va0.val", "1")
        if cmd == 0x0A:
            screen.send_text("page prefilament")
            screen.update_text("prefilament.filamentlength.txt",
                               "%d" % screen._filament_load_length)
            screen.update_text("prefilament.filamentspeed.txt",
                               "%d" % screen._filament_load_feedrate)
        if cmd == 0xD:
            screen.send_text("page multiset")


class ResumePrintProcessor(CommandProcessor):
    def process(self, message, screen):
        if message.command_data[0] == 0x1:
            screen.update_numeric("restFlag1", "0")
            screen.send_text("page wait")
            screen._run_gcode("M24",
                              lambda: screen.send_text("page printpause"))


class PausePrintProcessor(CommandProcessor):
    def process(self, message, screen):
        cmd = message.command_data[0]
        if cmd == 0x1:
            screen.send_text("page pauseconfirm")
        if cmd == 0xF1:
            screen.update_numeric("restFlag1", "1")
            screen.send_text("page wait")
            screen._run_gcode("M25",
                              lambda: screen.send_text("page printpause"))


class StopPrintProcessor(CommandProcessor):
    def process(self, message, screen):
        cmd = message.command_data[0]
        if cmd in (0x1, 0xF1):
            screen.send_text("page wait")
            screen._run_gcode("CANCEL_PRINT",
                              lambda: screen.send_text("page main"))


class HardwareTestProcessor(CommandProcessor):
    def process(self, message, screen):
        pass


class SettingBackProcessor(CommandProcessor):
    def _restart_if_config_needed(self, screen):
        s = screen._get_status("configfile")
        config = s.get("configfile", {})
        if config.get("save_config_pending"):
            screen.send_text("page wait")
            screen._run_gcode("SAVE_CONFIG")

    def process(self, message, screen):
        cmd = message.command_data[0]
        if cmd == 0x01:
            screen._run_gcode(
                "Z_OFFSET_APPLY_PROBE\nG1 F1000 Z.2",
                lambda: self._restart_if_config_needed(screen))
        if cmd == 0x7:
            screen._version = message.command_data[1]


class PrintFileProcessor(CommandProcessor):
    def process(self, message, screen):
        cmd = message.command_data[0]
        if cmd == 0x01:
            screen._run_gcode(
                'SDCARD_PRINT_FILE FILENAME="%s"' % screen._requested_file)
        if cmd == 0x0B:
            direction = message.command_data[1]
            if direction == 0x0:
                screen._file_page_number = max(
                    0, screen._file_page_number - 1)
                screen.update_file_list()
            if direction == 0x1:
                if ((screen._file_page_number + 1) * screen._file_per_page
                        < len(screen._file_list)):
                    screen._file_page_number += 1
                screen.update_file_list()


class SelectFileProcessor(CommandProcessor):
    def process(self, message, screen):
        screen.update_text("askprint.t0.txt", "")
        screen.update_text("printpause.t0.txt", "")

        max_file = len(screen._file_list) - 1
        requested = message.command_data[0] - 1

        if requested > max_file:
            screen.send_text("beep 2000")
        else:
            fname = screen._file_list[requested][0]
            screen.update_text("askprint.t0.txt", fname)
            screen.update_text("printpause.t0.txt", fname)
            screen._requested_file = fname
            screen.send_text("page askprint")


class PowerContinueProcessor(CommandProcessor):
    def process(self, message, screen):
        if message.command_data[0] == 0x03:
            screen.send_text("page multiset")


class PrintFilesProcessor(CommandProcessor):
    def process(self, message, screen):
        screen.update_text("printcnfirm.t0.txt", "")
        screen.update_text("printpause.t0.txt", "")

        max_file = len(screen._file_list) - 1
        requested = (screen._file_page_number * screen._file_per_page
                     + message.command_data[0])

        if requested > max_file:
            screen.send_text("beep 2000")
        else:
            fname = screen._file_list[requested][0]
            screen.update_text("printcnfirm.t0.txt", fname)
            screen.update_text("printpause.t0.txt", fname)
            screen._requested_file = fname

            screen.update_numeric("printcnfirm.t1.font", 0)
            screen.update_numeric("printcnfirm.t2.font", 0)
            screen.update_numeric("printcnfirm.t3.font", 0)
            screen.update_text("printcnfirm.t1.txt", "Print this model?")
            screen.update_text("printcnfirm.t2.txt", "Confirm")
            screen.update_text("printcnfirm.t3.txt", "Cancel")
            screen.send_text("page printcnfirm")


class FilamentLoadProcessor(CommandProcessor):
    def process(self, message, screen):
        s = screen._get_status("gcode_move")
        pos = s.get("gcode_move", {}).get("gcode_position", [0, 0, 0, 0])
        current_e = pos[3] if isinstance(pos, list) and len(pos) > 3 else 0

        cmd = message.command_data[0]
        if cmd == 0x01:  # unload
            screen._run_gcode(
                "G0 E%.2f F%.2f" % (
                    current_e - screen._filament_load_length,
                    screen._filament_load_feedrate))
        if cmd == 0x02:  # load
            screen._run_gcode(
                "G0 E%.2f F%.2f" % (
                    current_e + screen._filament_load_length,
                    screen._filament_load_feedrate))


class Heater0LoadEnterProcessor(CommandProcessor):
    def process(self, message, screen):
        length = ((message.command_data[0] & 0xff) << 8
                  | ((message.command_data[0] >> 8) & 0xff))
        screen._filament_load_length = length
        screen.update_text("prefilament.filamentlength.txt",
                           "%d" % screen._filament_load_length)


class Heater1LoadEnterProcessor(CommandProcessor):
    def process(self, message, screen):
        feedrate = ((message.command_data[0] & 0xff) << 8
                    | ((message.command_data[0] >> 8) & 0xff))
        screen._filament_load_feedrate = feedrate
        screen.update_text("prefilament.filamentspeed.txt",
                           "%d" % screen._filament_load_feedrate)


class PrintConfirmProcessor(CommandProcessor):
    def process(self, message, screen):
        screen._run_gcode(
            'SDCARD_PRINT_FILE FILENAME="%s"' % screen._requested_file)


# ── Processor registry ──────────────────────────────────────────────

COMMAND_PROCESSORS = [
    MainPageProcessor(DGUS_KEY_MAIN_PAGE),
    BedLevelProcessor(DGUS_KEY_BED_LEVEL),
    TempScreenProcessor(DGUS_KEY_TEMP_SCREEN),
    CoolScreenProcessor(DGUS_KEY_COOL_SCREEN),
    AxisPageSelectProcessor(DGUS_KEY_AXIS_PAGE_SELECT),
    ZAxisMoveKeyProcessor(DGUS_KEY_ZAXIS_MOVE_KEY),
    YAxisMoveKeyProcessor(DGUS_KEY_YAXIS_MOVE_KEY),
    XAxisMoveKeyProcessor(DGUS_KEY_XAXIS_MOVE_KEY),
    Heater0KeyProcessor(DGUS_KEY_HEATER0_TEMP_ENTER),
    HeaterBedKeyProcessor(DGUS_KEY_HOTBED_TEMP_ENTER),
    AdjustmentProcessor(DGUS_KEY_ADJUSTMENT),
    SettingScreenProcessor(DGUS_KEY_SETTING_SCREEN),
    ResumePrintProcessor(DGUS_KEY_RESUME_PRINT),
    PausePrintProcessor(DGUS_KEY_PAUSE_PRINT),
    StopPrintProcessor(DGUS_KEY_STOP_PRINT),
    HardwareTestProcessor(DGUS_KEY_HARDWARE_TEST),
    SettingBackProcessor(DGUS_KEY_SETTING_BACK_KEY),
    PrintFileProcessor(DGUS_KEY_PRINT_FILE),
    SelectFileProcessor(DGUS_KEY_SELECT_FILE),
    PowerContinueProcessor(DGUS_KEY_POWER_CONTINUE),
    PrintFilesProcessor(DGUS_KEY_PRINT_FILES),
    FilamentLoadProcessor(DGUS_KEY_FILAMENT_LOAD),
    Heater0LoadEnterProcessor(DGUS_KEY_HEATER0_LOAD_ENTER),
    Heater1LoadEnterProcessor(DGUS_KEY_HEATER1_LOAD_ENTER),
    PrintConfirmProcessor(DGUS_KEY_PRINT_CONFIRM),
]


# ── Entry point ─────────────────────────────────────────────────────

def load_config(config_path):
    """Load INI config file, return dict of settings."""
    cfg = configparser.ConfigParser()
    cfg.read(config_path)
    settings = {}
    if cfg.has_section("neptune_screen"):
        s = cfg["neptune_screen"]
        if "socket" in s:
            settings["socket"] = s["socket"]
        if "serial_bridge" in s:
            settings["bridge"] = s["serial_bridge"]
        if "variant" in s:
            settings["variant"] = s["variant"]
        if "led" in s:
            settings["led"] = s["led"]
        if "heater" in s:
            settings["heater"] = [
                h.strip() for h in s["heater"].split(",")]
        if "gcodes_dir" in s:
            settings["gcodes_dir"] = s["gcodes_dir"]
        if "update_interval" in s:
            settings["update_interval"] = s.getfloat("update_interval")
        if "logging" in s:
            settings["debug"] = s.getboolean("logging")
    return settings


def main():
    parser = argparse.ArgumentParser(
        description="Neptune 3 DGUS screen daemon for Klipper")
    parser.add_argument(
        "-c", "--config", default=None,
        help="Path to config file (default: neptune_screen.cfg next to script)")
    parser.add_argument(
        "-s", "--socket", default=None,
        help="Klipper Unix socket path (default: /tmp/klippy_uds)")
    parser.add_argument(
        "-b", "--bridge", default=None,
        help="serial_bridge name from printer.cfg (default: screen)")
    parser.add_argument(
        "-v", "--variant", default=None,
        choices=["3Pro", "3Plus", "3Max"],
        help="Neptune variant (default: 3Pro)")
    parser.add_argument(
        "-l", "--led", default=None,
        help="LED name for toggle button (default: my_led)")
    parser.add_argument(
        "-d", "--debug", action="store_true", default=None,
        help="Enable debug logging")
    args = parser.parse_args()

    # Load config file (defaults to neptune_screen.cfg beside the script)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = args.config or os.path.join(script_dir, "neptune_screen.cfg")
    cfg = {}
    if os.path.isfile(config_path):
        cfg = load_config(config_path)
        log.debug("Loaded config from %s", config_path)

    # CLI args override config file; fall back to defaults
    sock = os.path.expanduser(
        args.socket or cfg.get("socket", "~/printer_data/comms/klippy.sock"))
    bridge = args.bridge or cfg.get("bridge", "screen")
    variant = args.variant or cfg.get("variant", "3Pro")
    led = args.led or cfg.get("led", "my_led")
    heaters = cfg.get("heater", ["extruder", "heater_bed"])
    gcodes_dir = cfg.get("gcodes_dir")
    update_interval = cfg.get("update_interval", 2)
    debug = args.debug if args.debug is not None else cfg.get("debug", False)

    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    client = KlipperClient(sock)
    screen = NeptuneScreen(client, bridge, variant, led, heaters,
                           gcodes_dir, update_interval)

    def shutdown(signum, frame):
        log.info("Shutting down...")
        screen.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    while True:
        try:
            log.info("Connecting to Klipper at %s ...", sock)
            screen.start()
        except (ConnectionRefusedError, FileNotFoundError):
            log.warning("Klipper not ready, retrying in 5s...")
            time.sleep(5)
        except KeyboardInterrupt:
            break
        except Exception:
            log.exception("Unexpected error, retrying in 5s...")
            screen.stop()
            time.sleep(5)
            # Re-create client for clean reconnect
            client = KlipperClient(sock)
            screen = NeptuneScreen(client, bridge, variant, led, heaters,
                                   gcodes_dir, update_interval)


if __name__ == "__main__":
    main()
