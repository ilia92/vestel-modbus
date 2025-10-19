# Vestel EVC04 Modbus Reader/Exporter

A Python tool for reading and controlling Vestel EVC04 electric vehicle chargers via Modbus TCP protocol.

## Features

- **Multiple output formats**: Human-readable, Prometheus metrics, and JSON
- **Read comprehensive charger data**: Status, power consumption, current/voltage per phase, session information
- **Control charging current**: Set dynamic and failsafe current limits
- **Flexible configuration**: INI file or command-line arguments
- **Compatible with pymodbus 3.x+**

## Requirements

- Python 3.6+
- pymodbus 3.x or later

## Installation

1. Clone this repository:
```bash
git clone <your-repo-url>
cd vestel-modbus
```

2. Install dependencies:
```bash
pip install pymodbus
```

3. Create configuration file (optional but recommended):
```bash
cp vestel_modbus.ini.example vestel_modbus.ini
```

## Configuration

Create a `vestel_modbus.ini` file in the same directory as the script:

```ini
[vestel]
ip = 192.168.1.100
port = 502
unit = 1
base = 0
timeout = 2.0
```

### Configuration Parameters

| Parameter | Description | Default |
|-----------|-------------|---------|
| `ip` | IP address of the Vestel EVC04 charger | *Required* |
| `port` | Modbus TCP port | 502 |
| `unit` | Modbus unit/slave ID | 1 |
| `base` | Address base (0 or 1) | 0 |
| `timeout` | TCP connection timeout in seconds | 2.0 |

The script will automatically look for:
1. `vestel_modbus.ini` in the same folder as the script
2. `~/.config/vestel_modbus.ini` as fallback

All parameters can be overridden via command-line arguments.

## Usage

### Basic Reading

```bash
# Human-readable output (default)
./vestel.py

# JSON output
./vestel.py --format=json

# Prometheus metrics format
./vestel.py --format=prometheus
```

### Command-Line Overrides

```bash
# Override IP address
./vestel.py --ip 192.168.1.50

# Specify custom config file
./vestel.py --config /path/to/config.ini

# Combine options
./vestel.py --ip 192.168.1.50 --format=json --timeout 5.0
```

### Setting Charging Current

```bash
# Set both dynamic and failsafe current to 16A
./vestel.py --set-current 16

# Set only dynamic current
./vestel.py --set-dynamic-current 16

# Set only failsafe current
./vestel.py --set-failsafe-current 10
```

## Output Formats

### Human-Readable (default)

```
== Identity ==
Serial:              VEST1234567890
Max Power:           22000 W (22.00 kW)
Phases:              3-phase

== States ==
Chargepoint State:   Charging
Charging State:      Charging
Equipment State:     Running
Cable State:         CableConnected_VehicleLocked
...
```

### JSON

Structured JSON output with organized sections:

```json
{
  "identity": {
    "serial": "VEST1234567890",
    "max_power_w": 22000,
    "max_power_kw": 22.0,
    "phases": "3-phase"
  },
  "states": {
    "chargepoint": {
      "code": 2,
      "name": "Charging"
    },
    ...
  },
  "electrical": {
    "current": {
      "l1_a": 15.23,
      "l2_a": 15.18,
      "l3_a": 15.25
    },
    ...
  }
}
```

### Prometheus

Metrics format suitable for Prometheus scraping:

```
# HELP vestel_max_power_watts Maximum power in watts
# TYPE vestel_max_power_watts gauge
vestel_max_power_watts{serial="VEST1234567890"} 22000

# HELP vestel_current_amperes Current in amperes per phase
# TYPE vestel_current_amperes gauge
vestel_current_amperes{serial="VEST1234567890",phase="1"} 15.23
...
```

## Data Available

The tool reads the following information from your charger:

### Identity
- Serial number
- Maximum power capacity
- Phase configuration (1-phase or 3-phase)

### Status
- Chargepoint state (Available, Charging, Faulted, etc.)
- Charging state
- Equipment state
- Cable connection state
- Fault codes

### Electrical Measurements
- Current per phase (L1, L2, L3) in amperes
- Voltage per phase in volts
- Active power per phase and total in watts
- Energy meter reading in kWh

### Session Information
- Session energy consumption in kWh
- Session duration in seconds
- Session maximum current

### Current Limits
- EVSE minimum/maximum current
- Cable maximum current
- Dynamic current setting
- Failsafe current and timeout

## Modbus Register Map

The script reads from the following register ranges:

| Register | Type | Description |
|----------|------|-------------|
| 100-124 | Input | Device serial number (string) |
| 400-404 | Input | Power and phase configuration |
| 1000-1018 | Input | States and electrical measurements |
| 1020-1036 | Input | Power and energy readings |
| 1100-1106 | Input | Current limits |
| 1502-1508 | Input | Session data |
| 2000-2002 | Holding | Failsafe current settings |
| 5004 | Holding | Dynamic charging current |

## Integration Examples

### Home Assistant via REST sensor

```yaml
sensor:
  - platform: rest
    resource: http://your-server/vestel-data.json
    name: "EV Charger"
    json_attributes:
      - electrical
      - states
      - session
    value_template: "{{ value_json.states.chargepoint.name }}"
```

### Prometheus Scraping

Configure prometheus.yml:

```yaml
scrape_configs:
  - job_name: 'vestel_charger'
    static_configs:
      - targets: ['your-server:port']
```

### Cron Job for Monitoring

```bash
# Add to crontab to log every 5 minutes
*/5 * * * * /path/to/vestel.py --format=json >> /var/log/vestel_charger.log
```

## Troubleshooting

### Connection Issues

```bash
# Test with increased timeout
./vestel.py --timeout 10.0

# Verify network connectivity
ping 192.168.1.100

# Check if Modbus port is open
nc -zv 192.168.1.100 502
```

### Common Errors

**"ERROR: IP not set"**
- Add IP to config file or use `--ip` argument

**"ERROR: Cannot connect"**
- Verify charger IP address and network connectivity
- Check if Modbus TCP is enabled on the charger
- Ensure firewall allows port 502

**"ERROR: failed writing current"**
- Check if the current value is within acceptable range
- Verify the charger is not in a locked state

## Command-Line Reference

```
usage: vestel.py [-h] [--config CONFIG] [--ip IP] [--port PORT] [--unit UNIT]
                 [--base {0,1}] [--timeout TIMEOUT]
                 [--format {human,prometheus,json}]
                 [--set-current SET_CURRENT]
                 [--set-dynamic-current SET_DYNAMIC_CURRENT]
                 [--set-failsafe-current SET_FAILSAFE_CURRENT]

optional arguments:
  -h, --help            show this help message and exit
  --config CONFIG       INI config file
  --ip IP               Override IP from config
  --port PORT           Override TCP port
  --unit UNIT           Override Modbus unit/slave ID
  --base {0,1}          Address base (0 or 1)
  --timeout TIMEOUT     TCP timeout seconds
  --format {human,prometheus,json}
                        Output format
  --set-current SET_CURRENT
                        Set both dynamic and failsafe charging current (A)
  --set-dynamic-current SET_DYNAMIC_CURRENT
                        Set dynamic charging current (A) to reg 5004
  --set-failsafe-current SET_FAILSAFE_CURRENT
                        Set failsafe charging current (A) to reg 2000
```

## License

MIT License - feel free to use and modify as needed.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## Disclaimer

This tool is provided as-is. Always ensure you understand the implications of changing charging parameters. Incorrect settings may damage your vehicle or charger. Use at your own risk.

## Author

Created for monitoring and controlling Vestel EVC04 electric vehicle chargers via Modbus TCP protocol.
