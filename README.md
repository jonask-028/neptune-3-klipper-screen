# Neptune 3 Klipper Screen

Standalone DGUS DWIN touchscreen daemon for Elegoo Neptune 3 Pro/Plus/Max
running Klipper firmware with the
[serial_bridge](https://github.com/Klipper3d/klipper/pull/6444) module.

Communicates with Klipper via its Unix socket API — no internal Klipper
module required.

## Requirements

- Klipper with `serial_bridge` support (PR #6444)
- Python 3.7+
- The serial bridge configured in `printer.cfg` for the DGUS screen UART

## Klipper printer.cfg

Add the serial bridge section to your `printer.cfg`:

```ini
[serial_bridge screen]
uart_pin: PA3    # adjust to your board's screen UART TX pin
rx_pin: PA2      # adjust to your board's screen UART RX pin
baud: 115200
```

## Installation

```bash
# Clone onto your Klipper host (e.g. Raspberry Pi)
cd ~
git clone https://github.com/jonask-028/neptune-3-klipper-screen.git

# Install the systemd service
cd neptune-3-klipper-screen
./install.sh
```

## Usage

```bash
# Run directly
python3 neptune_screen.py -b screen -v 3Pro

# Or with debug logging
python3 neptune_screen.py -b screen -v 3Pro -d
```

### Command-line options

| Option            | Default           | Description                              |
| ----------------- | ----------------- | ---------------------------------------- |
| `-s`, `--socket`  | `/tmp/klippy_uds` | Klipper Unix socket path                 |
| `-b`, `--bridge`  | `screen`          | `serial_bridge` name from printer.cfg    |
| `-v`, `--variant` | `3Pro`            | Neptune variant: `3Pro`, `3Plus`, `3Max` |
| `-d`, `--debug`   | off               | Enable debug logging                     |

## Service management

```bash
# Check status
sudo systemctl status neptune-screen@$USER

# View logs
journalctl -u neptune-screen@$USER -f

# Restart
sudo systemctl restart neptune-screen@$USER
```

## Architecture

```
┌──────────────┐     Unix socket      ┌─────────────┐
│ neptune_     │◄────────────────────►│   Klipper   │
│ screen.py    │  JSON-RPC (\x03)     │  (klippy)   │
│              │                      └──────┬──────┘
│  ┌─────────┐ │                             │
│  │ DGUS    │ │  serial_bridge API          │ MCU serial
│  │ parser  │ │  (subscribe/send)           │
│  └─────────┘ │                      ┌──────┴──────┐
│              │                      │ serial_     │
│  ┌─────────┐ │                      │ bridge.c    │
│  │ command │ │                      │ (firmware)  │
│  │ procs   │ │                      └──────┬──────┘
│  └─────────┘ │                             │ UART
└──────────────┘                      ┌──────┴──────┐
                                      │ DGUS DWIN   │
                                      │ touchscreen │
                                      └─────────────┘
```

## License

GNU GPLv3 — see source file headers for copyright details.
