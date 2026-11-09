#!/usr/bin/env python3
"""
ET-1810 Fatal Error EEPROM Fix via USB
Writes 0x00 to addresses 288-291 (0x0120-0x0123) directly via USB,
bypassing WIC Reset's protection on the active error slots.

Usage: sudo python3 fix_eeprom_usb.py
"""
import sys, time, struct

try:
    import usb.core, usb.util
except ImportError:
    print("pyusb fehlt: pip3 install pyusb")
    sys.exit(1)

VENDOR  = 0x04b8
PRODUCT = 0x130a
EP_OUT  = 0x04
EP_IN   = 0x85

# Addresses to clear (Fatal Error 0x791E slots 1+2)
PATCHES = {288: 0x00, 289: 0x00, 290: 0x00, 291: 0x00}

def find_printer():
    dev = usb.core.find(idVendor=VENDOR, idProduct=PRODUCT)
    if dev is None:
        print("FEHLER: ET-1810 nicht gefunden. USB-Kabel prüfen.")
        sys.exit(1)
    print(f"Gefunden: {dev.product} (Bus {dev.bus}, Addr {dev.address})")
    return dev

def claim(dev):
    # On macOS, detach_kernel_driver may not be needed and can cause issues
    try:
        if dev.is_kernel_driver_active(0):
            dev.detach_kernel_driver(0)
            print("macOS-Druckertreiber temporär getrennt")
    except (NotImplementedError, AttributeError, usb.core.USBError):
        pass
    try:
        dev.set_configuration()
    except usb.core.USBError:
        pass  # already configured
    usb.util.claim_interface(dev, 0)
    print("Interface 0 belegt")

def drain(dev):
    """Non-blocking drain with very short timeout."""
    for _ in range(5):
        try:
            dev.read(EP_IN, 4096, 50)
        except Exception:
            break

def xchg(dev, data, wait=0.4):
    """Write data and collect response with timeout."""
    dev.write(EP_OUT, data, 3000)
    time.sleep(wait)
    out = []
    # Read up to 3 chunks with short timeout
    for _ in range(3):
        try:
            chunk = bytes(dev.read(EP_IN, 4096, 300))
            out.append(chunk)
            if len(chunk) < 4096:
                break
        except Exception:
            break
    return b''.join(out)

def read_eeprom_addr(dev, addr):
    """Read one EEPROM byte. Returns int or None."""
    drain(dev)
    # Try BDC EEPROM read (various format variants)
    for fmt in [
        f'@BDC PS\r\nEA:{addr:04X};\r\n'.encode(),
        f'@BDC PS\r\nER:{addr:04X};\r\n'.encode(),
    ]:
        r = xchg(dev, fmt, wait=0.4)
        if r and b'@BDC ST2' not in r and b'EE:' in r:
            # Parse EE:AAAAVV response
            idx = r.find(b'EE:')
            if idx >= 0:
                try:
                    val = int(r[idx+7:idx+9], 16)
                    return val
                except Exception:
                    pass
    return None

def write_eeprom_addr(dev, addr, val):
    """Write one EEPROM byte. Returns True on success."""
    drain(dev)
    attempts = [
        # BDC text format variants
        f'@BDC PS\r\nEW:{addr:04X}:{val:02X};\r\n'.encode(),
        f'@BDC PS\r\nEW:{addr:04X}{val:02X};\r\n'.encode(),
        # ESC(g service command: W = write, addr LE, value
        b'\x1b(g\x04\x00\x57' + struct.pack('<H', addr) + bytes([val]),
        # ESC(g with 0x54 ('T') command byte
        b'\x1b(g\x04\x00\x54' + struct.pack('<H', addr) + bytes([val]),
    ]
    for cmd in attempts:
        r = xchg(dev, cmd, wait=0.4)
        drain(dev)
        # Check if write confirmed (no BDC ST2 = something else happened)
        if r and b'@BDC ST2' not in r:
            print(f"  [{addr}] write response: {repr(r[:60])}")
            return True
        # Verify by reading back
        readback = read_eeprom_addr(dev, addr)
        if readback == val:
            return True
    return None  # Could not verify

def main():
    print("=" * 50)
    print("ET-1810 Fatal Error EEPROM Fix")
    print("=" * 50)

    dev = find_printer()
    claim(dev)

    # Initialize — short drain only, no blocking read
    print("\nInitialisiere Drucker-Verbindung...")
    drain(dev)
    print("Sende EJL init...")
    r = xchg(dev, b'\x1b\x01@EJL 1284.4\n@EJL\n', wait=0.6)
    print(f"  Init-Antwort: {len(r)} bytes")
    drain(dev)

    # Try to disable auto-status
    xchg(dev, b'\x1b\x01@EJL\nSET ASTATUS=OFF\n', wait=0.3)
    drain(dev)

    # Read current values at target addresses
    print("\nLese aktuelle Werte...")
    for addr in sorted(PATCHES.keys()):
        val = read_eeprom_addr(dev, addr)
        if val is not None:
            print(f"  [{addr:4d}/0x{addr:04X}] = 0x{val:02X} "
                  f"({'FEHLER' if val != 0 else 'ok'})")
        else:
            print(f"  [{addr:4d}/0x{addr:04X}] = (Lesezugriff nicht möglich)")

    print("\nSchreibe 0x00 auf Fehler-Adressen 288-291...")
    for addr, val in sorted(PATCHES.items()):
        result = write_eeprom_addr(dev, addr, val)
        if result is True:
            print(f"  [{addr:4d}] = 0x00  ✓")
        elif result is None:
            print(f"  [{addr:4d}] = 0x00  (nicht verifizierbar)")
        else:
            print(f"  [{addr:4d}] FEHLER beim Schreiben")

    print("\nVerifiziere...")
    all_ok = True
    for addr in sorted(PATCHES.keys()):
        val = read_eeprom_addr(dev, addr)
        if val is not None:
            ok = val == PATCHES[addr]
            print(f"  [{addr:4d}] = 0x{val:02X}  {'✓' if ok else '✗ IMMER NOCH FALSCH'}")
            if not ok:
                all_ok = False
        else:
            print(f"  [{addr:4d}] = (nicht lesbar - Schreiben unbekannt)")
            all_ok = False

    print()
    if all_ok:
        print("✓ EEPROM erfolgreich gepatcht!")
        print("  Drucker ausschalten, 10 Sek warten, einschalten.")
    else:
        print("✗ Schreiben nicht bestätigt.")
        print("  Mögliche Ursache: Drucker verweigert Schreibzugriff auf aktive Fehler-Slots.")
        print("  => CH341A Hardware-Programmer empfohlen.")

    usb.util.release_interface(dev, 0)

if __name__ == '__main__':
    main()
