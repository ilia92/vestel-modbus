#!/usr/bin/env python3
# Vestel EVC04 Modbus Reader/Exporter (pymodbus 3.x+)
# - External INI config
# - --format=human|prometheus|json  (default: human)
# - Prometheus metrics include label serial="..."
# - --set-current <amps> writes dynamic charging current (reg 5004)
# - --set-failsafe-current <amps> writes failsafe current (reg 2000)

import argparse, configparser, os, sys, json
from pymodbus.client import ModbusTcpClient

# ----------------------- Defaults & CLI -----------------------

# Try local config first, then fall back to user config
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOCAL_CFG_PATH = os.path.join(SCRIPT_DIR, "vestel_modbus.ini")
USER_CFG_PATH = os.path.expanduser("~/.config/vestel_modbus.ini")

def get_default_config_path():
    """Return local config if it exists, otherwise user config path"""
    if os.path.isfile(LOCAL_CFG_PATH):
        return LOCAL_CFG_PATH
    return USER_CFG_PATH

DEF_CFG_PATH = get_default_config_path()

def parse_args():
    p = argparse.ArgumentParser(description="Vestel EVC04 Modbus reader/exporter")
    p.add_argument("--config", default=DEF_CFG_PATH, help=f"INI config file (default: {DEF_CFG_PATH})")
    p.add_argument("--ip", help="Override IP from config")
    p.add_argument("--port", type=int, help="Override TCP port (default from config)")
    p.add_argument("--unit", type=int, help="Override Modbus unit/slave ID (default from config)")
    p.add_argument("--base", type=int, choices=[0,1], help="Address base (0 or 1)")
    p.add_argument("--timeout", type=float, help="TCP timeout seconds")
    p.add_argument("--format", choices=["human","prometheus","json"], default="human", help="Output format")
    p.add_argument("--set-current", type=int, help="Set both dynamic (reg 5004) and failsafe (reg 2000) charging current (A)")
    p.add_argument("--set-dynamic-current", type=int, help="Set dynamic charging current (A) to reg 5004")
    p.add_argument("--set-failsafe-current", type=int, help="Set failsafe charging current (A) to reg 2000")
    return p.parse_args()

def load_config(path):
    cfg = {"ip": None, "port": 502, "unit": 1, "base": 0, "timeout": 2.0}
    if os.path.isfile(path):
        cp = configparser.ConfigParser()
        cp.read(path)
        if cp.has_section("vestel"):
            sec = cp["vestel"]
            cfg["ip"] = sec.get("ip", fallback=cfg["ip"])
            cfg["port"] = sec.getint("port", fallback=cfg["port"])
            cfg["unit"] = sec.getint("unit", fallback=cfg["unit"])
            cfg["base"] = sec.getint("base", fallback=cfg["base"])
            cfg["timeout"] = sec.getfloat("timeout", fallback=cfg["timeout"])
    return cfg

def merge_overrides(cfg, args):
    if args.ip is not None: cfg["ip"] = args.ip
    if args.port is not None: cfg["port"] = args.port
    if args.unit is not None: cfg["unit"] = args.unit
    if args.base is not None: cfg["base"] = args.base
    if args.timeout is not None: cfg["timeout"] = args.timeout
    return cfg

# ----------------------- Modbus helpers -----------------------

def _adj(addr, base):
    return addr - base

def _ok(rr):
    return (rr is not None) and (not rr.isError())

def _get_unit_param(**kwargs):
    """Try different parameter names for unit/slave/device_id"""
    # Check what parameters the method accepts
    import inspect
    frame = inspect.currentframe()
    try:
        # Get the calling function
        caller_locals = frame.f_back.f_locals
        method = caller_locals.get('method')
        if method and hasattr(method, '__code__'):
            params = method.__code__.co_varnames
            if 'device_id' in params:
                return 'device_id'
            elif 'slave' in params:
                return 'slave'
            else:
                return 'unit'
    finally:
        del frame
    return 'unit'  # fallback

def _call_modbus_method(cli, method_name, address, count=None, value=None, unit=1, **kwargs):
    """Call modbus method with appropriate unit parameter name and parameters"""
    method = getattr(cli, method_name)
    
    # Prepare base arguments
    base_args = {'address': address}
    
    # Add method-specific arguments
    if method_name in ['read_input_registers', 'read_holding_registers', 'read_coils', 'read_discrete_inputs']:
        if count is not None:
            base_args['count'] = count
    elif method_name in ['write_register', 'write_coil']:
        if value is not None:
            base_args['value'] = value
    elif method_name in ['write_registers', 'write_coils']:
        if value is not None:
            base_args['values'] = value
    
    # Try different parameter names based on pymodbus version
    for param_name in ['device_id', 'slave', 'unit']:
        try:
            return method(**base_args, **{param_name: unit}, **kwargs)
        except TypeError as e:
            if "unexpected keyword argument" in str(e) and param_name in str(e):
                continue
            else:
                raise
    
    # If all else fails, try without any unit parameter (shouldn't happen but just in case)
    return method(**base_args, **kwargs)

def read_input_u16(cli, addr, unit, base):
    rr = _call_modbus_method(cli, 'read_input_registers', _adj(addr,base), 1, unit=unit)
    return rr.registers[0] if _ok(rr) else None

def read_input_u32(cli, addr, unit, base):
    rr = _call_modbus_method(cli, 'read_input_registers', _adj(addr,base), 2, unit=unit)
    if not _ok(rr): return None
    hi, lo = rr.registers
    return (hi << 16) | lo

def read_hold_u16(cli, addr, unit, base):
    rr = _call_modbus_method(cli, 'read_holding_registers', _adj(addr,base), 1, unit=unit)
    return rr.registers[0] if _ok(rr) else None

def write_hold_u16(cli, addr, value, unit, base):
    rr = _call_modbus_method(cli, 'write_register', _adj(addr,base), value=value & 0xFFFF, unit=unit)
    return _ok(rr)

def read_input_str(cli, start_addr, reg_count, unit, base):
    rr = _call_modbus_method(cli, 'read_input_registers', _adj(start_addr,base), reg_count, unit=unit)
    if not _ok(rr): return None
    be, le = bytearray(), bytearray()
    for w in rr.registers:
        hi, lo = (w >> 8) & 0xFF, w & 0xFF
        be.extend((hi, lo)); le.extend((lo, hi))
    clean = lambda b: bytes(x for x in b if x != 0).decode(errors="ignore").strip()
    s_be, s_le = clean(be), clean(le)
    return s_le if len(s_le) > len(s_be) else s_be

def prom_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("\"","\\\"").replace("\n","\\n")

# ----------------------- State maps -----------------------

CP_STATE = {0:"Available",1:"Preparing",2:"Charging",3:"SuspendedEVSE",4:"SuspendedEV",
            5:"Finishing",6:"Reserved",7:"Unavailable",8:"Faulted"}
CH_STATE = {0:"NotCharging",1:"Charging"}
EQ_STATE = {0:"Initializing",1:"Running",2:"Fault",3:"Disabled",4:"Updating"}
CAB_STATE= {0:"CableNotConnected",1:"CableConnected_NoVehicle",2:"CableConnected_Vehicle",3:"CableConnected_VehicleLocked"}

# ----------------------- Snapshot -----------------------

def read_snapshot(cli, unit, base):
    s = {}
    s["serial"]      = read_input_str(cli, 100, 25, unit, base) or ""
    s["cp_power_w"]  = read_input_u32(cli, 400, unit, base)
    s["phases"]      = read_input_u16(cli, 404, unit, base)
    s["cp_state"]    = read_input_u16(cli, 1000, unit, base)
    s["charging_state"] = read_input_u16(cli, 1001, unit, base)
    s["equip_state"] = read_input_u16(cli, 1002, unit, base)
    s["cable_state"] = read_input_u16(cli, 1004, unit, base)
    s["fault_code"]  = read_input_u32(cli, 1006, unit, base)
    s["i_l1_ma"]     = read_input_u16(cli, 1008, unit, base)
    s["i_l2_ma"]     = read_input_u16(cli, 1010, unit, base)
    s["i_l3_ma"]     = read_input_u16(cli, 1012, unit, base)
    s["v_l1_v"]      = read_input_u16(cli, 1014, unit, base)
    s["v_l2_v"]      = read_input_u16(cli, 1016, unit, base)
    s["v_l3_v"]      = read_input_u16(cli, 1018, unit, base)
    s["p_tot_w"]     = read_input_u32(cli, 1020, unit, base)
    s["p_l1_w"]      = read_input_u32(cli, 1024, unit, base)
    s["p_l2_w"]      = read_input_u32(cli, 1028, unit, base)
    s["p_l3_w"]      = read_input_u32(cli, 1032, unit, base)
    s["meter_01kwh"] = read_input_u32(cli, 1036, unit, base)
    s["sess_max_A"]  = read_input_u16(cli, 1100, unit, base)
    s["evse_min_A"]  = read_input_u16(cli, 1102, unit, base)
    s["evse_max_A"]  = read_input_u16(cli, 1104, unit, base)
    s["cable_max_A"] = read_input_u16(cli, 1106, unit, base)
    s["sess_energy_Wh"] = read_input_u32(cli, 1502, unit, base)
    s["sess_duration_s"]= read_input_u32(cli, 1508, unit, base)
    s["dyn_current_A"]= read_hold_u16(cli, 5004, unit, base)
    s["failsafe_A"]  = read_hold_u16(cli, 2000, unit, base)
    s["failsafe_t_s"]= read_hold_u16(cli, 2002, unit, base)
    return s

# ----------------------- Human output -----------------------

def print_human(s):
    print("== Identity ==")
    print(f"Serial:              {s['serial']}")
    if s.get("cp_power_w") is not None:
        print(f"Max Power:           {s['cp_power_w']} W ({(s['cp_power_w'] or 0)/1000.0:.2f} kW)")
    print(f"Phases:              {'3-phase' if s.get('phases')==1 else '1-phase'}")

    print("\n== States ==")
    print(f"Chargepoint State:   {CP_STATE.get(s['cp_state'], s['cp_state'])}")
    print(f"Charging State:      {CH_STATE.get(s['charging_state'], s['charging_state'])}")
    print(f"Equipment State:     {EQ_STATE.get(s['equip_state'], s['equip_state'])}")
    print(f"Cable State:         {CAB_STATE.get(s['cable_state'], s['cable_state'])}")
    print(f"EVSE Fault Code:     {s['fault_code']}")

    print("\n== Electricals ==")
    for ph in ("l1","l2","l3"):
        i_ma = s.get(f"i_{ph}_ma")
        v = s.get(f"v_{ph}_v")
        if i_ma is not None:
            i_a = i_ma / 1000.0
            print(f"Current {ph.upper()}:       {i_a:.2f} A")
        if v is not None:
            print(f"Voltage {ph.upper()}:       {v} V")

    if s.get("p_tot_w") is not None:
        print(f"Active Power Total:  {s['p_tot_w']/1000.0:.2f} kW")

    if s.get("meter_01kwh") is not None:
        print(f"Meter Reading:       {s['meter_01kwh']/10.0:.1f} kWh")

    print("\n== Limits & Session ==")
    print(f"EVSE Min/Max Current: {s['evse_min_A']} / {s['evse_max_A']} A")
    print(f"Cable Max Current:    {s['cable_max_A']} A")
    print(f"Session Max Current:  {s['sess_max_A']} A")
    print(f"Session Energy:       {s['sess_energy_Wh']/1000.0:.3f} kWh")
    print(f"Session Duration:     {s['sess_duration_s']} s")

    print("\n== Current Settings ==")
    print(f"Dynamic Current:     {s['dyn_current_A']} A")
    print(f"Failsafe Current:    {s['failsafe_A']} A")
    print(f"Failsafe Timeout:    {s['failsafe_t_s']} s")

# ----------------------- Prometheus output -----------------------

def print_prometheus(s):
    serial = prom_escape(s['serial'])
    
    # Helper function to output a metric with proper formatting
    def output_metric(name, value, help_text, metric_type="gauge", extra_labels=""):
        if value is not None:
            print(f"# HELP {name} {help_text}")
            print(f"# TYPE {name} {metric_type}")
            print(f'{name}{{serial="{serial}"{extra_labels}}} {value}')
    
    # Identity metrics
    output_metric("vestel_max_power_watts", s.get("cp_power_w"), "Maximum power in watts")
    output_metric("vestel_phases", s.get("phases"), "Phase configuration (0=1-phase, 1=3-phase)")
    
    # State metrics
    output_metric("vestel_chargepoint_state", s.get("cp_state"), "Chargepoint state")
    output_metric("vestel_charging_state", s.get("charging_state"), "Charging state")
    output_metric("vestel_equipment_state", s.get("equip_state"), "Equipment state")
    output_metric("vestel_cable_state", s.get("cable_state"), "Cable state")
    output_metric("vestel_fault_code", s.get("fault_code"), "EVSE fault code")
    
    # Electrical measurements
    for phase, phase_num in [("l1", 1), ("l2", 2), ("l3", 3)]:
        current_ma = s.get(f"i_{phase}_ma")
        voltage_v = s.get(f"v_{phase}_v")
        power_w = s.get(f"p_{phase}_w")
        
        if current_ma is not None:
            output_metric("vestel_current_amperes", current_ma / 1000.0, 
                         "Current in amperes per phase", extra_labels=f',phase="{phase_num}"')
        if voltage_v is not None:
            output_metric("vestel_voltage_volts", voltage_v, 
                         "Voltage in volts per phase", extra_labels=f',phase="{phase_num}"')
        if power_w is not None:
            output_metric("vestel_power_watts", power_w, 
                         "Power in watts per phase", extra_labels=f',phase="{phase_num}"')
    
    output_metric("vestel_total_power_watts", s.get("p_tot_w"), "Total active power in watts")
    output_metric("vestel_meter_reading_kwh", 
                  s.get("meter_01kwh") / 10.0 if s.get("meter_01kwh") is not None else None, 
                  "Meter reading in kWh")
    
    # Current limits
    output_metric("vestel_evse_min_current_amperes", s.get("evse_min_A"), "EVSE minimum current in amperes")
    output_metric("vestel_evse_max_current_amperes", s.get("evse_max_A"), "EVSE maximum current in amperes")
    output_metric("vestel_cable_max_current_amperes", s.get("cable_max_A"), "Cable maximum current in amperes")
    output_metric("vestel_session_max_current_amperes", s.get("sess_max_A"), "Session maximum current in amperes")
    
    # Session data
    output_metric("vestel_session_energy_wh", s.get("sess_energy_Wh"), "Session energy in Wh")
    output_metric("vestel_session_duration_seconds", s.get("sess_duration_s"), "Session duration in seconds")
    
    # Current settings
    output_metric("vestel_dynamic_current_amperes", s.get("dyn_current_A"), "Dynamic current setting in amperes")
    output_metric("vestel_failsafe_current_amperes", s.get("failsafe_A"), "Failsafe current setting in amperes")
    output_metric("vestel_failsafe_timeout_seconds", s.get("failsafe_t_s"), "Failsafe timeout in seconds")

# ----------------------- JSON output -----------------------

def print_json(s):
    """Output snapshot data as JSON with enhanced/computed fields"""
    output = {
        "identity": {
            "serial": s["serial"],
            "max_power_w": s.get("cp_power_w"),
            "max_power_kw": round(s["cp_power_w"] / 1000.0, 2) if s.get("cp_power_w") else None,
            "phases": "3-phase" if s.get("phases") == 1 else "1-phase",
            "phases_raw": s.get("phases")
        },
        "states": {
            "chargepoint": {
                "code": s.get("cp_state"),
                "name": CP_STATE.get(s["cp_state"], "Unknown")
            },
            "charging": {
                "code": s.get("charging_state"),
                "name": CH_STATE.get(s["charging_state"], "Unknown")
            },
            "equipment": {
                "code": s.get("equip_state"),
                "name": EQ_STATE.get(s["equip_state"], "Unknown")
            },
            "cable": {
                "code": s.get("cable_state"),
                "name": CAB_STATE.get(s["cable_state"], "Unknown")
            },
            "fault_code": s.get("fault_code")
        },
        "electrical": {
            "current": {
                "l1_a": round(s["i_l1_ma"] / 1000.0, 2) if s.get("i_l1_ma") is not None else None,
                "l2_a": round(s["i_l2_ma"] / 1000.0, 2) if s.get("i_l2_ma") is not None else None,
                "l3_a": round(s["i_l3_ma"] / 1000.0, 2) if s.get("i_l3_ma") is not None else None
            },
            "voltage": {
                "l1_v": s.get("v_l1_v"),
                "l2_v": s.get("v_l2_v"),
                "l3_v": s.get("v_l3_v")
            },
            "power": {
                "l1_w": s.get("p_l1_w"),
                "l2_w": s.get("p_l2_w"),
                "l3_w": s.get("p_l3_w"),
                "total_w": s.get("p_tot_w"),
                "total_kw": round(s["p_tot_w"] / 1000.0, 2) if s.get("p_tot_w") is not None else None
            },
            "meter_reading_kwh": round(s["meter_01kwh"] / 10.0, 1) if s.get("meter_01kwh") is not None else None
        },
        "limits": {
            "evse_min_a": s.get("evse_min_A"),
            "evse_max_a": s.get("evse_max_A"),
            "cable_max_a": s.get("cable_max_A"),
            "session_max_a": s.get("sess_max_A")
        },
        "session": {
            "energy_wh": s.get("sess_energy_Wh"),
            "energy_kwh": round(s["sess_energy_Wh"] / 1000.0, 3) if s.get("sess_energy_Wh") is not None else None,
            "duration_s": s.get("sess_duration_s")
        },
        "settings": {
            "dynamic_current_a": s.get("dyn_current_A"),
            "failsafe_current_a": s.get("failsafe_A"),
            "failsafe_timeout_s": s.get("failsafe_t_s")
        }
    }
    
    print(json.dumps(output, indent=2))

# ----------------------- Main -----------------------

def main():
    args = parse_args()
    cfg = merge_overrides(load_config(args.config), args)
    if not cfg["ip"]:
        sys.exit("ERROR: IP not set (use --ip or config file).")

    client = ModbusTcpClient(host=cfg["ip"], port=cfg["port"], timeout=cfg["timeout"])
    if not client.connect():
        sys.exit(f"ERROR: Cannot connect to {cfg['ip']}:{cfg['port']}")

    try:
        snap = read_snapshot(client, cfg["unit"], cfg["base"])

        if args.set_current is not None:
            desired = args.set_current
            # Set both dynamic and failsafe current
            ok_dynamic = write_hold_u16(client, 5004, desired, cfg["unit"], cfg["base"])
            ok_failsafe = write_hold_u16(client, 2000, desired, cfg["unit"], cfg["base"])
            if not ok_dynamic: 
                sys.exit(f"ERROR: failed writing {desired} A to dynamic current register 5004")
            if not ok_failsafe:
                sys.exit(f"ERROR: failed writing {desired} A to failsafe current register 2000")
            snap["dyn_current_A"] = read_hold_u16(client, 5004, cfg["unit"], cfg["base"])
            snap["failsafe_A"] = read_hold_u16(client, 2000, cfg["unit"], cfg["base"])
            print(f"Set both dynamic and failsafe current → {snap['dyn_current_A']} A / {snap['failsafe_A']} A")

        if args.set_dynamic_current is not None:
            desired = args.set_dynamic_current
            ok = write_hold_u16(client, 5004, desired, cfg["unit"], cfg["base"])
            if not ok: sys.exit(f"ERROR: failed writing {desired} A to 5004")
            snap["dyn_current_A"] = read_hold_u16(client, 5004, cfg["unit"], cfg["base"])
            print(f"Set dynamic current → {snap['dyn_current_A']} A")

        if args.set_failsafe_current is not None:
            desired = args.set_failsafe_current
            ok = write_hold_u16(client, 2000, desired, cfg["unit"], cfg["base"])
            if not ok: sys.exit(f"ERROR: failed writing {desired} A to 2000")
            snap["failsafe_A"] = read_hold_u16(client, 2000, cfg["unit"], cfg["base"])
            print(f"Set failsafe current → {snap['failsafe_A']} A")

        if args.format == "prometheus":
            print_prometheus(snap)
        elif args.format == "json":
            print_json(snap)
        else:
            print_human(snap)

    finally:
        client.close()

if __name__ == "__main__":
    main()
