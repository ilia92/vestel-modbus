#!/usr/bin/env python3
# Vestel EVC04 Modbus Reader/Exporter (pymodbus 3.x+) - OPTIMIZED VERSION
# - External INI config
# - --format=human|prometheus|json  (default: human)
# - Prometheus metrics include label serial="..."
# - --set-current <amps> writes dynamic charging current (reg 5004)
# - --set-failsafe-current <amps> writes failsafe current (reg 2000)
# 
# OPTIMIZATION: Uses bulk reads to minimize modbus messages from ~20+ to just 4 reads

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

def write_hold_u16(cli, addr, value, unit, base):
    rr = _call_modbus_method(cli, 'write_register', _adj(addr,base), value=value & 0xFFFF, unit=unit)
    return _ok(rr)

def read_input_str_from_regs(regs):
    """Extract string from register array"""
    be, le = bytearray(), bytearray()
    for w in regs:
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

# ----------------------- OPTIMIZED Snapshot -----------------------

def read_snapshot(cli, unit, base):
    """
    OPTIMIZED: Read all data with minimal modbus queries using bulk reads
    
    Original: ~20+ individual register reads
    Optimized: 6 bulk reads (aggressive optimization with larger reads):
      1. Input registers 100-124 (25 regs) - Serial number
      2. Input registers 400-404 (5 regs) - Power config (cp_power_w is u32, phases is u16)
      3. Input registers 1000-1106 (107 regs) - States, currents, voltages, powers, meter, limits
      4. Input registers 1502-1509 (8 regs) - Session energy and duration
      5. Holding registers 2000-2002 (3 regs) - Failsafe settings
      6. Holding register 5004 (1 reg) - Dynamic current
    """
    s = {}
    
    # === BULK READ 1: Serial number (input registers 100-124) ===
    rr1 = _call_modbus_method(cli, 'read_input_registers', _adj(100, base), 25, unit=unit)
    if _ok(rr1):
        s["serial"] = read_input_str_from_regs(rr1.registers)
    else:
        s["serial"] = ""
    
    # === BULK READ 2: Power config block (input registers 400-404) ===
    # 400-401: cp_power_w (u32), 404: phases (u16)
    rr2 = _call_modbus_method(cli, 'read_input_registers', _adj(400, base), 5, unit=unit)
    if _ok(rr2):
        regs = rr2.registers
        # Extract u32 from position 0-1 (addr 400-401)
        s["cp_power_w"] = (regs[0] << 16) | regs[1]
        # Extract u16 from position 4 (addr 404)
        s["phases"] = regs[4] if len(regs) > 4 else None
    else:
        s["cp_power_w"] = None
        s["phases"] = None
    
    # === BULK READ 3: MEGA block (input registers 1000-1106) ===
    # This is aggressive: reading 107 registers in one shot to cover:
    # - States, fault, currents, voltages, powers, meter (1000-1037)
    # - Gap with unused registers (1038-1099)
    # - Current limits (1100-1106)
    # This trades some wasted bandwidth for fewer transactions
    rr3 = _call_modbus_method(cli, 'read_input_registers', _adj(1000, base), 107, unit=unit)
    if _ok(rr3):
        regs = rr3.registers
        # States (1000-1006)
        s["cp_state"] = regs[0]           # 1000
        s["charging_state"] = regs[1]     # 1001
        s["equip_state"] = regs[2]        # 1002
        s["cable_state"] = regs[4]        # 1004
        # Fault code u32 (1006-1007)
        s["fault_code"] = (regs[6] << 16) | regs[7]
        
        # Currents u16 (1008, 1010, 1012)
        s["i_l1_ma"] = regs[8]            # 1008
        s["i_l2_ma"] = regs[10]           # 1010
        s["i_l3_ma"] = regs[12]           # 1012
        
        # Voltages u16 (1014, 1016, 1018)
        s["v_l1_v"] = regs[14]            # 1014
        s["v_l2_v"] = regs[16]            # 1016
        s["v_l3_v"] = regs[18]            # 1018
        
        # Powers u32 (1020-1021, 1024-1025, 1028-1029, 1032-1033)
        s["p_tot_w"] = (regs[20] << 16) | regs[21]    # 1020-1021
        s["p_l1_w"] = (regs[24] << 16) | regs[25]     # 1024-1025
        s["p_l2_w"] = (regs[28] << 16) | regs[29]     # 1028-1029
        s["p_l3_w"] = (regs[32] << 16) | regs[33]     # 1032-1033
        
        # Meter reading u32 (1036-1037)
        s["meter_01kwh"] = (regs[36] << 16) | regs[37]
        
        # Current limits (1100-1106) - offset by 100 from start
        s["sess_max_A"] = regs[100]       # 1100
        s["evse_min_A"] = regs[102]       # 1102
        s["evse_max_A"] = regs[104]       # 1104
        s["cable_max_A"] = regs[106]      # 1106
    else:
        # Set all to None if bulk read fails
        for key in ["cp_state", "charging_state", "equip_state", "cable_state", "fault_code",
                    "i_l1_ma", "i_l2_ma", "i_l3_ma", "v_l1_v", "v_l2_v", "v_l3_v",
                    "p_l1_w", "p_l2_w", "p_l3_w", "p_tot_w", "meter_01kwh",
                    "sess_max_A", "evse_min_A", "evse_max_A", "cable_max_A"]:
            s[key] = None
    
    # === BULK READ 4: Session data (input registers 1502-1509) ===
    # Reading 8 registers to cover both energy (1502-1503) and duration (1508-1509)
    rr4 = _call_modbus_method(cli, 'read_input_registers', _adj(1502, base), 8, unit=unit)
    if _ok(rr4):
        regs = rr4.registers
        s["sess_energy_Wh"] = (regs[0] << 16) | regs[1]      # 1502-1503
        s["sess_duration_s"] = (regs[6] << 16) | regs[7]     # 1508-1509
    else:
        s["sess_energy_Wh"] = None
        s["sess_duration_s"] = None
    
    # === BULK READ 5: Failsafe settings (holding registers 2000-2002) ===
    rr5 = _call_modbus_method(cli, 'read_holding_registers', _adj(2000, base), 3, unit=unit)
    if _ok(rr5):
        s["failsafe_A"] = rr5.registers[0]    # 2000
        s["failsafe_t_s"] = rr5.registers[2]  # 2002
    else:
        s["failsafe_A"] = None
        s["failsafe_t_s"] = None
    
    # === BULK READ 6: Dynamic current (holding register 5004) ===
    rr6 = _call_modbus_method(cli, 'read_holding_registers', _adj(5004, base), 1, unit=unit)
    if _ok(rr6):
        s["dyn_current_A"] = rr6.registers[0]  # 5004
    else:
        s["dyn_current_A"] = None
    
    return s

# ----------------------- Human output -----------------------

def print_human(s):
    print("== Identity ==")
    print(f"Serial:              {s['serial']}")
    print(f"Max Power:           {s.get('cp_power_w')} W ({s.get('cp_power_w', 0)/1000:.2f} kW)")
    print(f"Phases:              {'3-phase' if s.get('phases')==1 else '1-phase'}")
    print()
    print("== States ==")
    print(f"Chargepoint State:   {CP_STATE.get(s.get('cp_state'), 'Unknown')}")
    print(f"Charging State:      {CH_STATE.get(s.get('charging_state'), 'Unknown')}")
    print(f"Equipment State:     {EQ_STATE.get(s.get('equip_state'), 'Unknown')}")
    print(f"Cable State:         {CAB_STATE.get(s.get('cable_state'), 'Unknown')}")
    print(f"EVSE Fault Code:     {s.get('fault_code')}")
    print()
    print("== Electricals ==")
    print(f"Current L1:       {s.get('i_l1_ma', 0)/1000:.2f} A")
    print(f"Voltage L1:       {s.get('v_l1_v')} V")
    print(f"Current L2:       {s.get('i_l2_ma', 0)/1000:.2f} A")
    print(f"Voltage L2:       {s.get('v_l2_v')} V")
    print(f"Current L3:       {s.get('i_l3_ma', 0)/1000:.2f} A")
    print(f"Voltage L3:       {s.get('v_l3_v')} V")
    print(f"Active Power Total:  {s.get('p_tot_w', 0)/1000:.2f} kW")
    print(f"Meter Reading:       {s.get('meter_01kwh', 0)/10:.1f} kWh")
    print()
    print("== Limits & Session ==")
    print(f"EVSE Min/Max Current: {s.get('evse_min_A')} / {s.get('evse_max_A')} A")
    print(f"Cable Max Current:    {s.get('cable_max_A')} A")
    print(f"Session Max Current:  {s.get('sess_max_A')} A")
    print(f"Session Energy:       {s.get('sess_energy_Wh', 0)/1000:.3f} kWh")
    print(f"Session Duration:     {s.get('sess_duration_s')} s")
    print()
    print("== Current Settings ==")
    print(f"Dynamic Current:     {s.get('dyn_current_A')} A")
    print(f"Failsafe Current:    {s.get('failsafe_A')} A")
    print(f"Failsafe Timeout:    {s.get('failsafe_t_s')} s")
    print()

# ----------------------- Prometheus output -----------------------

def print_prometheus(s):
    """Output snapshot data in Prometheus exposition format"""
    serial = prom_escape(s["serial"])
    seen_metrics = set()
    
    def output_metric(name, value, help_text, metric_type="gauge", extra_labels=""):
        """Helper to output a Prometheus metric with proper formatting"""
        if value is None:
            return
        
        # Only print TYPE and HELP once per metric name
        if name not in seen_metrics:
            seen_metrics.add(name)
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
            # Re-read to confirm
            rr_dyn = _call_modbus_method(client, 'read_holding_registers', _adj(5004, cfg["base"]), 1, unit=cfg["unit"])
            rr_fail = _call_modbus_method(client, 'read_holding_registers', _adj(2000, cfg["base"]), 1, unit=cfg["unit"])
            snap["dyn_current_A"] = rr_dyn.registers[0] if _ok(rr_dyn) else None
            snap["failsafe_A"] = rr_fail.registers[0] if _ok(rr_fail) else None
            print(f"Set both dynamic and failsafe current → {snap['dyn_current_A']} A / {snap['failsafe_A']} A")

        if args.set_dynamic_current is not None:
            desired = args.set_dynamic_current
            ok = write_hold_u16(client, 5004, desired, cfg["unit"], cfg["base"])
            if not ok: sys.exit(f"ERROR: failed writing {desired} A to 5004")
            rr = _call_modbus_method(client, 'read_holding_registers', _adj(5004, cfg["base"]), 1, unit=cfg["unit"])
            snap["dyn_current_A"] = rr.registers[0] if _ok(rr) else None
            print(f"Set dynamic current → {snap['dyn_current_A']} A")

        if args.set_failsafe_current is not None:
            desired = args.set_failsafe_current
            ok = write_hold_u16(client, 2000, desired, cfg["unit"], cfg["base"])
            if not ok: sys.exit(f"ERROR: failed writing {desired} A to 2000")
            rr = _call_modbus_method(client, 'read_holding_registers', _adj(2000, cfg["base"]), 1, unit=cfg["unit"])
            snap["failsafe_A"] = rr.registers[0] if _ok(rr) else None
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
