# check_tasmota_sensor

A Nagios/Icinga plugin for monitoring [Tasmota](https://tasmota.github.io/docs/)
devices via Tasmota's HTTP command API (`GET http://<host>/cm?cmnd=<command>`).

## Features

- **Four kinds of checks** (`--check`)
  - `sensor`: compare a numeric value from `Status 8` (`StatusSNS`) against
    `-w`/`-c` thresholds. Entities are addressed as `<Module>.<Field>`, e.g.
    `BME280.Temperature` or `ENERGY.Power` -- Tasmota nests sensor readings
    one level under the module/sensor name. Unlike most plugins, `-c` may be
    smaller than `-w`: whichever of the two is larger decides whether higher
    or lower values are considered worse.
  - `time`: compare the device's own reported time (the `Time` field,
    present in every `Status` response) against the local clock (default) or
    an NTP server (`--time-server`), in seconds of offset.
  - `binary`: evaluate a boolean expression (`and`/`or`/`not`/parentheses)
    over one or more `POWER`-style relay states from `Status 11`
    (`StatusSTS`), e.g. `--binary-expr "POWER1 and not POWER2"` on a
    multi-relay device, or plain `POWER` on a single-relay one.
  - `text`: match a value from `Status 8` (same `<Module>.<Field>`
    addressing as `sensor`) against a Python regular expression (`--regex`).
- **`--list`**: instead of `--check`, print the available sensor fields (as
  `<Module>.<Field>`) and `POWER`-style relay states, with their current
  value, so you know what to pass to `--entity`/`--text-entity`/
  `--binary-expr`.
- **Authentication**: if a web admin password is set on the device, pass
  `-u`/`-P` -- Tasmota expects these as `user`/`password` *query
  parameters* on every `/cm` request (not HTTP Basic auth), so they are
  sent that way here too.

### A note on units of measurement

Unlike ESPHome, Tasmota does not report a unit of measurement per sensor
field in general. Automatic `--uom` detection is therefore only attempted
for fields literally named `Temperature` or `Pressure`/`PressureAtSeaLevel`,
using Tasmota's global `TempUnit`/`PressureUnit` hints. For anything else,
pass `--uom` explicitly if you want one.

### A note on the `time` check

Tasmota's `Time` field has no UTC offset suffix -- it is local wall-clock
time, per the device's configured timezone, not UTC. This plugin assumes
that naive time string is in *this host's own* local timezone (the same
assumption Python's `datetime.timestamp()` makes for a naive datetime), so
the `time` check is only accurate if the monitoring host and the Tasmota
device are configured for the same effective timezone.

## Installation

```console
$ pip install .
```

This installs a `check_tasmota_sensor` console script.

## Usage

```console
$ check_tasmota_sensor --help
```

### Examples

```console
# Numeric sensor threshold check
check_tasmota_sensor -H tasmota.local --check sensor --entity BME280.Temperature -w 30 -c 35

# Power draw threshold check, with an explicit unit (Tasmota doesn't report one for ENERGY.Power)
check_tasmota_sensor -H tasmota.local --check sensor --entity ENERGY.Power -w 2000 -c 2500 --uom W

# Time offset check against the local clock
check_tasmota_sensor -H tasmota.local --check time -w 5 -c 30

# Logical combination of relay states on a multi-relay device
check_tasmota_sensor -H tasmota.local --check binary --binary-expr 'POWER1 and not POWER2'

# Regex match against a sensor field
check_tasmota_sensor -H tasmota.local --check text --text-entity 'DS18B20-1.Id' --regex '^01212F'

# List all entities you could target with the above
check_tasmota_sensor -H tasmota.local --list
```

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
