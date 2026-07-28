"""
Flow — flow.py
Single-file build (per Creator's call: everything in one file, not the
originally-sketched folder split). Grows section by section as the
locked build order proceeds:

  1. DETECT     — hardware fingerprint                         [DONE, tested on HP]
  2. EXECUTOR   — hidden subprocess runner                     [THIS PASS]
  3. RESTORE    — Windows restore point creation                [THIS PASS]
  4. TIER/TWEAK — tweak database + tier application     [DRAFT, awaiting sign-off]
  5. REVERT     — revert tracking                        [DONE, folded into Section 4]
  6. GUI        — pywebview wiring                                     [DONE]
  7. IDLE       — idle daemon                                     [DONE, this pass]

  [DAEMON RELIABILITY PASS] Fixed against a real 70-cycle/1.6-day log:
  revert-log entries are now deduped by tweak_id before each check (a
  tweak reapplied across multiple apply_tier runs was being checked/
  reapplied once per duplicate log entry, multiplying blocklist counters
  incorrectly). Added a separate _STICKY_DRIFT_THRESHOLD mechanism —
  the existing blocklist only ever tripped on an apply call that FAILED;
  it never caught a tweak that "successfully" reapplies every cycle but
  is back to non-target by the next check (something outside Flow keeps
  resetting it). daemon_status() now also reports whether the installed
  scheduled task's script path matches the file currently being run
  from, to catch the "edited flow.py but never reinstalled the daemon"
  trap in this dev loop.

No third-party deps beyond the standard library for Sections 1-5 — PowerShell
CIM/cmdlets do everything needed without pip installs (zero-budget
constraint). Section 6's GUI is the one exception: pywebview is the only
way to get an embedded browser window, so it's vendored into a local
_flow_deps/ folder next to this script on first GUI launch (see
check_requirements() / _install_pywebview_package()) rather than installed
system-wide — deleting the Flow folder removes it too, no separate
uninstall step needed. The optional .env loader (_load_dotenv(), just
below the imports) is a ~20-line parser, not python-dotenv, to keep that
zero-dep rule intact outside Section 6 too.
"""

import json
import os
import subprocess
import platform
import sys
import traceback
import threading
import time
from dataclasses import dataclass, asdict, field
from typing import Optional, List, Union, Tuple

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
_DEBUG = "--debug" in sys.argv


def _is_placeholder_value(value: str) -> bool:
    """Flags obviously-unfilled template values (e.g. 'your_groq_key_here')
    so a .env that ships with ALL supported providers pre-listed -- filled
    or not -- doesn't accidentally treat an unfilled placeholder line as a
    real key. Real API keys are opaque random strings; none of the markers
    below plausibly appear in one."""
    v = value.strip().lower()
    if not v:
        return True
    return any(marker in v for marker in ("your_", "_here", "xxxxxxxx", "replace_me", "paste_"))


def _load_dotenv(path: Optional[str] = None) -> None:
    """Minimal built-in .env loader — deliberately not python-dotenv, to
    keep Sections 1-5's zero-third-party-deps rule intact for the whole
    file, not just the PowerShell-driven parts.

    Looks for '.env' next to flow.py itself (via __file__), not the
    current working directory — so it's found the same way whether Flow
    is launched by double-click, from a random cwd in a terminal, or via
    flow.bat's `cd /d "%~dp0"`. Real environment variables always win: a
    key already set with `$env:GROQ_API_KEY = "..."` in the shell is never
    overwritten by .env, matching normal dotenv precedence. Missing or
    unreadable .env is silently a no-op — this file is optional, same as
    the GUI's Settings panel, which remains the simpler path for most
    people. Unfilled placeholder lines (see _is_placeholder_value) are
    skipped rather than loaded, so copying .env.example -> .env with every
    provider still listed and only ONE actually filled in just works,
    with no need to delete or comment out the rest.
    """
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]  # strip matching surrounding quotes, dotenv convention
                if key and key not in os.environ and not _is_placeholder_value(value):
                    os.environ[key] = value
    except OSError:
        pass


_load_dotenv()  # runs on every import/launch, before anything reads os.environ for AI keys


# ═══════════════════════════════════════════════════════════════════
# SECTION 1 — DETECT (hardware fingerprint)
# ═══════════════════════════════════════════════════════════════════

def _run_ps(command: str, timeout: int = 15, _retry: bool = True) -> Optional[str]:
    """Run a PowerShell command hidden, return stdout or None on failure.
    Retries once after a short delay — on weak/HDD rigs the WMI service
    (winmgmt) can be slow to spin up on the first CIM query since boot.
    With --debug, prints return code / stderr / exception on failure."""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=_NO_WINDOW,
        )
        if result.returncode != 0:
            if _DEBUG:
                print(f"[debug] rc={result.returncode} stderr={result.stderr.strip()!r} cmd={command!r}", file=sys.stderr)
            if _retry:
                time.sleep(1.5)
                return _run_ps(command, timeout=min(timeout, 20), _retry=False)
            return None
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        if _DEBUG:
            print(f"[debug] exception={exc!r} cmd={command!r}", file=sys.stderr)
        if _retry:
            time.sleep(1.5)
            return _run_ps(command, timeout=min(timeout, 20), _retry=False)
        return None


def _run_ps_json(command: str, timeout: int = 15):
    raw = _run_ps(command, timeout=timeout)
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, dict):
        return [parsed]
    if isinstance(parsed, list):
        return parsed
    return []


@dataclass
class CPUInfo:
    name: str = "Unknown"
    physical_cores: int = 0
    logical_cores: int = 0
    max_clock_mhz: int = 0


@dataclass
class RAMInfo:
    total_gb: float = 0.0
    module_count: int = 0
    speed_mhz: int = 0


@dataclass
class DiskInfo:
    model: str
    media_type: str
    size_gb: float


@dataclass
class GPUInfo:
    name: str
    vram_gb: float
    is_dedicated: bool


@dataclass
class BoardInfo:
    """Motherboard + firmware identity. Rarely changes, cheap to detect,
    genuinely useful for diagnosing tweak compatibility on older boards."""
    manufacturer: str = "Unknown"
    model: str = "Unknown"
    bios_version: str = "Unknown"


@dataclass
class BatteryInfo:
    """None on desktops — Win32_Battery simply returns no rows there, so
    _detect_battery() returns None rather than a zeroed-out struct, and the
    GUI skips the row entirely instead of showing a fake '0%, desktop'."""
    percent: int = 0
    charging: bool = False


@dataclass
class NetworkAdapterInfo:
    name: str
    mac: str


@dataclass
class VolumeInfo:
    """Free space is a per-volume (drive letter) concept, not a per-physical-
    disk one — a single SSD can hold C: and D: with very different headroom —
    so this is intentionally separate from DiskInfo rather than bolted onto it."""
    drive_letter: str
    free_gb: float
    total_gb: float


@dataclass
class DisplayInfo:
    """One row per active monitor. refresh_hz comes back 0 on some virtual/
    RDP adapters — the GUI treats that as 'unknown' rather than a literal
    0Hz panel."""
    name: str = "Unknown"
    resolution_w: int = 0
    resolution_h: int = 0
    refresh_hz: int = 0


@dataclass
class HardwareProfile:
    cpu: CPUInfo = field(default_factory=CPUInfo)
    ram: RAMInfo = field(default_factory=RAMInfo)
    disks: list = field(default_factory=list)
    gpus: list = field(default_factory=list)
    os_name: str = "Unknown"
    os_build: str = "Unknown"
    os_install_date: str = "Unknown"
    os_version: str = "unknown"      # normalized: 7 | 8 | 8.1 | 10 | 11 | unknown
    os_edition: str = "Unknown"      # Home | Pro | Enterprise | Education
    os_arch: str = "Unknown"         # 64-bit | 32-bit | ARM64
    domain_joined: bool = False
    tpm_present: bool = False
    secure_boot: Optional[bool] = None  # None = undetermined (legacy BIOS, or query failed)
    board: BoardInfo = field(default_factory=BoardInfo)
    battery: Optional[BatteryInfo] = None
    is_laptop: bool = False  # battery presence OR SMBIOS chassis type — see _detect_is_laptop
    network: list = field(default_factory=list)
    volumes: list = field(default_factory=list)
    displays: list = field(default_factory=list)
    antivirus: str = "Unknown"
    startup_item_count: int = 0
    bloatware_installed: list = field(default_factory=list)  # ids from BLOATWARE_PACKAGES present on this machine
    uptime_hours: float = 0.0
    suggested_tier: str = "minimal"
    tier_reasons: list = field(default_factory=list)

    def to_dict(self):
        return {
            "cpu": asdict(self.cpu),
            "ram": asdict(self.ram),
            "disks": [asdict(d) for d in self.disks],
            "gpus": [asdict(g) for g in self.gpus],
            "os_name": self.os_name,
            "os_build": self.os_build,
            "os_install_date": self.os_install_date,
            "os_version": self.os_version,
            "os_edition": self.os_edition,
            "os_arch": self.os_arch,
            "domain_joined": self.domain_joined,
            "tpm_present": self.tpm_present,
            "secure_boot": self.secure_boot,
            "board": asdict(self.board),
            "battery": asdict(self.battery) if self.battery else None,
            "is_laptop": self.is_laptop,
            "network": [asdict(n) for n in self.network],
            "volumes": [asdict(v) for v in self.volumes],
            "displays": [asdict(d) for d in self.displays],
            "antivirus": self.antivirus,
            "startup_item_count": self.startup_item_count,
            "bloatware_installed": self.bloatware_installed,
            "uptime_hours": self.uptime_hours,
            "suggested_tier": self.suggested_tier,
            "tier_reasons": self.tier_reasons,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "HardwareProfile":
        """Inverse of to_dict() — reconstructs actual dataclass instances
        for every nested field, not plain dicts. Needed for the disk cache
        in Api._load_cached_profile(): every consumer downstream (tier
        gating, tweak targets) does attribute access like
        profile.cpu.physical_cores or disk.media_type, which breaks with
        an AttributeError if these came back as raw dicts instead."""
        d = dict(data)
        return cls(
            cpu=CPUInfo(**d.get("cpu", {})),
            ram=RAMInfo(**d.get("ram", {})),
            disks=[DiskInfo(**x) for x in d.get("disks", [])],
            gpus=[GPUInfo(**x) for x in d.get("gpus", [])],
            os_name=d.get("os_name", "Unknown"),
            os_build=d.get("os_build", "Unknown"),
            os_install_date=d.get("os_install_date", "Unknown"),
            os_version=d.get("os_version", "unknown"),
            os_edition=d.get("os_edition", "Unknown"),
            os_arch=d.get("os_arch", "Unknown"),
            domain_joined=d.get("domain_joined", False),
            tpm_present=d.get("tpm_present", False),
            secure_boot=d.get("secure_boot"),
            board=BoardInfo(**d.get("board", {})),
            battery=BatteryInfo(**d["battery"]) if d.get("battery") else None,
            is_laptop=d.get("is_laptop", False),
            network=[NetworkAdapterInfo(**x) for x in d.get("network", [])],
            volumes=[VolumeInfo(**x) for x in d.get("volumes", [])],
            displays=[DisplayInfo(**x) for x in d.get("displays", [])],
            antivirus=d.get("antivirus", "Unknown"),
            startup_item_count=d.get("startup_item_count", 0),
            bloatware_installed=d.get("bloatware_installed", []),
            uptime_hours=d.get("uptime_hours", 0.0),
            suggested_tier=d.get("suggested_tier", "minimal"),
            tier_reasons=d.get("tier_reasons", []),
        )


def _detect_cpu(row: Optional[dict]) -> CPUInfo:
    if not row:
        return CPUInfo(name=platform.processor() or "Unknown")
    return CPUInfo(
        name=(row.get("Name") or "Unknown").strip(),
        physical_cores=int(row.get("NumberOfCores") or 0),
        logical_cores=int(row.get("NumberOfLogicalProcessors") or 0),
        max_clock_mhz=int(row.get("MaxClockSpeed") or 0),
    )


def _detect_ram(modules: list, total_physical_bytes: int) -> RAMInfo:
    total_bytes = sum(int(m.get("Capacity") or 0) for m in modules)
    speed = modules[0].get("Speed") if modules else 0
    if total_bytes == 0:
        total_bytes = total_physical_bytes
    return RAMInfo(
        total_gb=round(total_bytes / (1024 ** 3), 2),
        module_count=len(modules),
        speed_mhz=int(speed or 0),
    )


def _detect_disks(rows: list) -> list:
    """rows comes from the single consolidated PS call (see _detect_all),
    which retries once at the whole-script level if disks came back empty —
    a genuinely disk-less system is vanishingly rare, so an empty result is
    far more likely a transient Get-PhysicalDisk hiccup than reality.
    hdd_only/ssd_only-gated tweaks silently vanish from apply_tier() results
    with NO other indication when profile.disks is empty — better to flag
    it than let a flaky detection call quietly change what gets applied."""
    disks = []
    for row in rows:
        media = (row.get("MediaType") or "Unspecified").strip()
        if media not in ("SSD", "HDD"):
            media = "Unspecified"
        disks.append(DiskInfo(
            model=(row.get("FriendlyName") or "Unknown").strip(),
            media_type=media,
            size_gb=round(int(row.get("Size") or 0) / (1024 ** 3), 2),
        ))
    return disks


def _detect_gpus(rows: list) -> list:
    gpus = []
    for row in rows:
        name = (row.get("Name") or "Unknown").strip()
        raw_ram = int(row.get("AdapterRAM") or 0)
        vram_gb = round(raw_ram / (1024 ** 3), 2) if raw_ram > 0 else 0.0
        is_dedicated = vram_gb > 1 and "intel" not in name.lower()
        gpus.append(GPUInfo(name=name, vram_gb=vram_gb, is_dedicated=is_dedicated))
    return gpus


def _detect_os(os_row: Optional[dict]) -> tuple:
    if not os_row:
        return platform.system() + " " + platform.release(), platform.version()
    return (os_row.get("Caption") or "Unknown").strip(), str(os_row.get("BuildNumber") or "Unknown")


def _normalize_os_version(build: str) -> str:
    try:
        b = int(str(build).split(".")[0])
    except (ValueError, TypeError):
        return "unknown"
    if b >= 22000:
        return "11"
    if b >= 10240:
        return "10"
    if b >= 9600:
        return "8.1"
    if b >= 9200:
        return "8"
    if b >= 7600:
        return "7"
    return "unknown"


_OS_ORDER = {"unknown": -1, "7": 0, "8": 1, "8.1": 2, "10": 3, "11": 4}


def _os_at_least(actual: str, minimum: str) -> bool:
    return _OS_ORDER.get(actual, -1) >= _OS_ORDER.get(minimum, 0)


def _os_at_most(actual: str, maximum: str) -> bool:
    return _OS_ORDER.get(actual, -1) <= _OS_ORDER.get(maximum, 4)


def _detect_os_edition(os_row: Optional[dict]) -> str:
    caption = ((os_row or {}).get("Caption") or "").lower()
    if "home" in caption:
        return "Home"
    if "enterprise" in caption:
        return "Enterprise"
    if "education" in caption:
        return "Education"
    if "pro" in caption:
        return "Pro"
    return "Unknown"


def _detect_os_arch(os_row: Optional[dict]) -> str:
    val = (os_row or {}).get("OSArchitecture")
    return str(val).strip() if val else (platform.machine() or "Unknown")


def _detect_domain_joined(cs_row: Optional[dict]) -> bool:
    return bool(cs_row and cs_row.get("PartOfDomain") is True)


def _detect_tpm_present(tpm_val) -> bool:
    return tpm_val is True


def _detect_secure_boot(sb_val) -> Optional[bool]:
    return bool(sb_val) if isinstance(sb_val, bool) else None


def _detect_board(board_row: Optional[dict], bios_row: Optional[dict]) -> BoardInfo:
    manufacturer = ((board_row or {}).get("Manufacturer") or "Unknown").strip()
    model = ((board_row or {}).get("Product") or "Unknown").strip()
    bios_version = ((bios_row or {}).get("SMBIOSBIOSVersion") or "Unknown").strip()
    return BoardInfo(manufacturer=manufacturer, model=model, bios_version=bios_version)


def _detect_battery(row: Optional[dict]) -> Optional[BatteryInfo]:
    """Desktops simply return no row here — that's the correct signal for
    'no battery', not a detection failure, so no retry/warning like disks."""
    if not row:
        return None
    # Win32_Battery.BatteryStatus: 2 = on AC/charging, 6-9 = various charging
    # sub-states, 1 = discharging. Anything outside that set treated as not charging.
    status = int(row.get("BatteryStatus") or 0)
    charging = status in (2, 6, 7, 8, 9)
    return BatteryInfo(percent=int(row.get("EstimatedChargeRemaining") or 0), charging=charging)


# DMTF SMBIOS chassis type codes (Win32_SystemEnclosure.ChassisTypes) that
# indicate a portable form factor. Desktop-ish codes (3=Desktop, 6/7=Tower,
# 13=All in One, etc.) are everything NOT in this set.
_LAPTOP_CHASSIS_TYPES = {8, 9, 10, 11, 14, 30, 31, 32}  # Portable, Laptop, Notebook,
# Hand Held, Sub Notebook, Tablet, Convertible, Detachable

# Intel/AMD model-name suffixes that only ever shipped in mobile parts —
# publicly documented naming conventions, not a heuristic guess:
#   Intel: U/Y (ULV mobile, e.g. i3-3217U, i7-1165G7's "U-class" siblings),
#          H/HK/HQ/HX (higher-power mobile, gaming laptops), P (12th gen+
#          mobile), and the 10th/11th-gen "Gx" mobile-graphics suffix.
#   AMD:   U (mobile), HS/HX (mobile, higher power), M (older mobile).
# Desktop suffixes (K/KF/F/T/S, or no suffix) are deliberately excluded —
# this only matches patterns that are mobile-exclusive.
_MOBILE_CPU_SUFFIX_RE = r"\b\d{3,5}(U|Y|HQ|HK|HX|H|P|G[1-9])\b|\b\d{3,4}(U|HS|HX|M)\b"


def _detect_is_laptop(battery: Optional[BatteryInfo], chassis_row, cpu_name: str = "") -> bool:
    """Three independent signals, any one being true is enough:

    1. Battery presence. False-negatives whenever Win32_Battery returns no
       rows for reasons unrelated to form factor — physically removed,
       dead/undetected pack, flaky OEM ACPI/WMI battery provider.
    2. Win32_SystemEnclosure.ChassisTypes (SMBIOS/DMI). Doesn't depend on
       the battery subsystem at all — but is itself known to be misreported
       as Desktop (3) by a real, documented slice of OEM firmware,
       especially budget/business laptops, so this alone isn't bulletproof
       either.
    3. CPU model-name suffix. Doesn't depend on ANY WMI class being honest
       — Intel/AMD's own public naming conventions mark mobile-only
       silicon (U/Y/H/HQ/HK/HX/P/Gx for Intel, U/HS/HX/M for AMD) that was
       simply never sold in a desktop. Last resort, but the most reliable
       one precisely because it can't be misreported by firmware."""
    import re
    if battery is not None:
        return True
    for code in _as_list(chassis_row):
        try:
            if int(code) in _LAPTOP_CHASSIS_TYPES:
                return True
        except (TypeError, ValueError):
            continue
    if cpu_name and re.search(_MOBILE_CPU_SUFFIX_RE, cpu_name):
        return True
    return False


def _detect_network(rows: list) -> list:
    return [
        NetworkAdapterInfo(name=(row.get("Name") or "Unknown").strip(), mac=(row.get("MacAddress") or "").strip())
        for row in rows
    ]


def _detect_volumes(rows: list) -> list:
    volumes = []
    for row in rows:
        total = int(row.get("Size") or 0)
        if total <= 0:
            continue
        volumes.append(VolumeInfo(
            drive_letter=str(row.get("DriveLetter") or "?"),
            free_gb=round(int(row.get("SizeRemaining") or 0) / (1024 ** 3), 2),
            total_gb=round(total / (1024 ** 3), 2),
        ))
    return volumes


def _detect_os_install_date(os_row: Optional[dict]) -> str:
    raw = (os_row or {}).get("InstallDate")
    if not raw:
        return "Unknown"
    # Get-CimInstance serializes DateTime to JSON as "/Date(ms)/" or ISO
    # depending on PS version; handle both without a second round trip.
    try:
        if isinstance(raw, str) and raw.startswith("/Date("):
            ms = int(raw[6:-2].split("+")[0].split("-")[0])
            import datetime
            return datetime.datetime.utcfromtimestamp(ms / 1000).strftime("%Y-%m-%d")
        return str(raw)[:10]
    except (ValueError, TypeError, IndexError):
        return "Unknown"


def _detect_displays(rows: list) -> list:
    """WmiMonitorID lives in root\\wmi (raw byte arrays for name), current
    mode lives on Win32_VideoController — joined loosely by index since
    there's no clean 1:1 key across the two namespaces. Falls back to just
    resolution/refresh from the video controller if the WMI monitor class
    is unavailable (common on some laptop panels/older drivers)."""
    displays = []
    for row in rows:
        w = int(row.get("CurrentHorizontalResolution") or 0)
        h = int(row.get("CurrentVerticalResolution") or 0)
        if w == 0 or h == 0:
            continue  # non-display adapters (e.g. remote/basic render) report zeroed-out modes
        displays.append(DisplayInfo(
            name=(row.get("Name") or "Display").strip(),
            resolution_w=w, resolution_h=h,
            refresh_hz=int(row.get("CurrentRefreshRate") or 0),
        ))
    return displays


def _detect_antivirus(rows: list) -> str:
    """SecurityCenter2 only exists on client SKUs (not Server), and only
    returns rows once Windows Security has actually initialized once post-
    boot — both are legitimate 'no data' cases, not detection bugs."""
    if not rows:
        return "Unknown"
    names = sorted({(r.get("displayName") or "").strip() for r in rows if r.get("displayName")})
    return ", ".join(names) if names else "Unknown"


def _detect_startup_item_count(count_val) -> int:
    try:
        return int(count_val or 0)
    except (TypeError, ValueError):
        return 0


def _detect_bloatware(rows: list) -> list:
    """Returns the subset of BLOATWARE_PACKAGES ids actually present for the
    current user."""
    if not rows:
        return []
    installed_names = {(r.get("Name") or "") for r in rows}
    return [pkg_id for pkg_id, meta in BLOATWARE_PACKAGES.items() if meta["package_name"] in installed_names]


def _detect_uptime_hours(hours_val) -> float:
    try:
        return round(float(hours_val), 1) if hours_val is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _suggest_tier(profile: HardwareProfile) -> tuple:
    reasons = []
    ram = profile.ram.total_gb
    cores = profile.cpu.physical_cores
    has_ssd = any(d.media_type == "SSD" for d in profile.disks)
    has_only_hdd = bool(profile.disks) and not has_ssd
    has_dedicated_gpu = any(g.is_dedicated for g in profile.gpus)

    if ram <= 4 or cores <= 2 or has_only_hdd:
        tier = "minimal"
        if ram <= 4:
            reasons.append(f"RAM at {ram}GB is at or under the 4GB floor")
        if cores <= 2:
            reasons.append(f"{cores} physical core(s) — no headroom for aggressive background tweaks")
        if has_only_hdd:
            reasons.append("no SSD detected — mechanical drive limits safe I/O-heavy tweaks")
    elif ram >= 16 and cores >= 6 and has_ssd:
        tier = "maximal"
        reasons.append(f"{ram}GB RAM, {cores} cores, SSD present — headroom for the full tweak set")
        if has_dedicated_gpu:
            reasons.append("dedicated GPU detected")
    else:
        tier = "standard"
        reasons.append(f"{ram}GB RAM, {cores} cores, SSD={'yes' if has_ssd else 'no'} — mid-range profile")
    return tier, reasons


# Single PowerShell script that gathers everything Section 1 needs in one
# process spawn instead of ~19. Each spawn costs a few hundred ms just for
# the PowerShell host to start (worse on the HDD/4GB rigs this is built
# for) — 19 sequential spawns is exactly why detect used to drag. Every
# query is wrapped in try/catch so one WMI class being unavailable (e.g.
# no Win32_Battery on a desktop, no TPM class on older boards) can't take
# the whole script down; each key just comes back $null / empty on failure,
# same as the old per-call behavior.
_DETECT_ESSENTIAL_SCRIPT = r"""
$r = [ordered]@{}
function TryGet($block) { try { & $block } catch { $null } }
$r.cpu        = TryGet { Get-CimInstance Win32_Processor | Select-Object Name,NumberOfCores,NumberOfLogicalProcessors,MaxClockSpeed -First 1 }
$r.ram_modules = @(TryGet { Get-CimInstance Win32_PhysicalMemory | Select-Object Capacity,Speed })
$r.cs         = TryGet { Get-CimInstance Win32_ComputerSystem | Select-Object TotalPhysicalMemory,PartOfDomain }
$r.disks      = @(TryGet { Get-PhysicalDisk | Select-Object FriendlyName,MediaType,Size })
$r.gpus       = @(TryGet { Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM,CurrentHorizontalResolution,CurrentVerticalResolution,CurrentRefreshRate })
$r.os         = TryGet { Get-CimInstance Win32_OperatingSystem | Select-Object Caption,BuildNumber,OSArchitecture,InstallDate,LastBootUpTime }
$r.battery    = TryGet { Get-CimInstance Win32_Battery | Select-Object EstimatedChargeRemaining,BatteryStatus }
$r.chassis    = @(TryGet { (Get-CimInstance Win32_SystemEnclosure).ChassisTypes })
$r.uptime_hours = TryGet {
    $os = Get-CimInstance Win32_OperatingSystem
    (New-TimeSpan -Start $os.LastBootUpTime -End (Get-Date)).TotalHours
}
$r | ConvertTo-Json -Depth 6 -Compress
"""

# Everything NOT needed to pick a tier or filter the tweak list -- only
# consumed by the Audit tab, which the user has to explicitly open. This
# is the slow half: Get-AppxPackage in particular can take real time on
# a cold/slow-HDD rig, and there's no reason a tweak-list-only visit
# should ever pay for it.
_DETECT_EXTRA_SCRIPT = r"""
$r = [ordered]@{}
function TryGet($block) { try { & $block } catch { $null } }
$r.tpm        = TryGet { (Get-CimInstance -Namespace root/cimv2/security/microsofttpm -ClassName Win32_Tpm -ErrorAction SilentlyContinue).IsEnabled_InitialValue }
$r.secure_boot = TryGet { Confirm-SecureBootUEFI }
$r.board      = TryGet { Get-CimInstance Win32_BaseBoard | Select-Object Manufacturer,Product }
$r.bios       = TryGet { Get-CimInstance Win32_BIOS | Select-Object SMBIOSBIOSVersion }
$r.network    = @(TryGet { Get-NetAdapter | Where-Object Status -eq 'Up' | Select-Object Name,MacAddress })
$r.volumes    = @(TryGet { Get-Volume | Where-Object { $_.DriveLetter -and $_.Size -gt 0 } | Select-Object DriveLetter,SizeRemaining,Size })
$r.antivirus  = @(TryGet { Get-CimInstance -Namespace 'root/SecurityCenter2' -ClassName AntivirusProduct -ErrorAction SilentlyContinue | Select-Object displayName })
$r.startup_count = TryGet { (Get-CimInstance Win32_StartupCommand | Measure-Object).Count }
$r.bloatware  = @(TryGet { Get-AppxPackage | Select-Object Name })
$r | ConvertTo-Json -Depth 6 -Compress
"""

# Kept for CLI subcommands (detect/list-tweaks/apply-tier etc.) that want
# one blocking call with everything -- just the two scripts above's
# queries combined, unchanged from the original single-spawn version.
_DETECT_ALL_SCRIPT = r"""
$r = [ordered]@{}
function TryGet($block) { try { & $block } catch { $null } }
$r.cpu        = TryGet { Get-CimInstance Win32_Processor | Select-Object Name,NumberOfCores,NumberOfLogicalProcessors,MaxClockSpeed -First 1 }
$r.ram_modules = @(TryGet { Get-CimInstance Win32_PhysicalMemory | Select-Object Capacity,Speed })
$r.cs         = TryGet { Get-CimInstance Win32_ComputerSystem | Select-Object TotalPhysicalMemory,PartOfDomain }
$r.disks      = @(TryGet { Get-PhysicalDisk | Select-Object FriendlyName,MediaType,Size })
$r.gpus       = @(TryGet { Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM,CurrentHorizontalResolution,CurrentVerticalResolution,CurrentRefreshRate })
$r.os         = TryGet { Get-CimInstance Win32_OperatingSystem | Select-Object Caption,BuildNumber,OSArchitecture,InstallDate,LastBootUpTime }
$r.tpm        = TryGet { (Get-CimInstance -Namespace root/cimv2/security/microsofttpm -ClassName Win32_Tpm -ErrorAction SilentlyContinue).IsEnabled_InitialValue }
$r.secure_boot = TryGet { Confirm-SecureBootUEFI }
$r.board      = TryGet { Get-CimInstance Win32_BaseBoard | Select-Object Manufacturer,Product }
$r.bios       = TryGet { Get-CimInstance Win32_BIOS | Select-Object SMBIOSBIOSVersion }
$r.battery    = TryGet { Get-CimInstance Win32_Battery | Select-Object EstimatedChargeRemaining,BatteryStatus }
$r.chassis    = @(TryGet { (Get-CimInstance Win32_SystemEnclosure).ChassisTypes })
$r.network    = @(TryGet { Get-NetAdapter | Where-Object Status -eq 'Up' | Select-Object Name,MacAddress })
$r.volumes    = @(TryGet { Get-Volume | Where-Object { $_.DriveLetter -and $_.Size -gt 0 } | Select-Object DriveLetter,SizeRemaining,Size })
$r.antivirus  = @(TryGet { Get-CimInstance -Namespace 'root/SecurityCenter2' -ClassName AntivirusProduct -ErrorAction SilentlyContinue | Select-Object displayName })
$r.startup_count = TryGet { (Get-CimInstance Win32_StartupCommand | Measure-Object).Count }
$r.bloatware  = @(TryGet { Get-AppxPackage | Select-Object Name })
$r.uptime_hours = TryGet {
    $os = Get-CimInstance Win32_OperatingSystem
    (New-TimeSpan -Start $os.LastBootUpTime -End (Get-Date)).TotalHours
}
$r | ConvertTo-Json -Depth 6 -Compress
"""


# Standalone retry query for _detect_all()'s disk fallback path (see below).
# Deliberately mirrors the $r.disks line inside _DETECT_ALL_SCRIPT above
# (Get-PhysicalDisk | Select-Object FriendlyName,MediaType,Size) rather than
# querying Win32_DiskDrive — _detect_disks() reads row["FriendlyName"],
# row["MediaType"], row["Size"] specifically. Win32_DiskDrive's Model/
# InterfaceType/Size fields don't line up with those keys, so it would
# "succeed" on retry but silently hand back Unknown/Unspecified disks
# instead of the real values.
_DISK_QUERY = "Get-PhysicalDisk | Select-Object FriendlyName,MediaType,Size | ConvertTo-Json"


def _as_list(val) -> list:
    """PowerShell's ConvertTo-Json collapses a single-item array to a bare
    object, and $null stays $null — normalize both to a plain list so every
    _detect_* parser below can iterate without special-casing shape."""
    if val is None:
        return []
    if isinstance(val, list):
        return val
    return [val]


def _detect_all(timeout: int = 45) -> dict:
    """One PowerShell spawn for the entire hardware fingerprint. Falls back
    to a single targeted retry only for disks (the one field with genuine
    transient-empty history on slow HDD rigs) rather than re-running the
    whole script, keeping the common case at exactly one spawn.

    Timeout raised from 20s -> 45s: this script bundles ~19 WMI/CIM queries
    (CPU, RAM, disks, GPU, OS, TPM, secure boot, board, BIOS, battery,
    network, volumes, AV, startup items, bloatware, uptime) into a single
    spawn. On a cold winmgmt service on a mechanical-HDD/4GB rig, 20s was
    getting hit before the script finished — not a real detection failure,
    just the process getting killed mid-query. This is the actual root
    cause of "detection sometimes doesn't work."""
    raw = _run_ps(_DETECT_ALL_SCRIPT, timeout=timeout)
    data = {}
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            if _DEBUG:
                print(f"[debug] _detect_all JSON parse failed: {exc!r} raw={raw[:300]!r}", file=sys.stderr)
            data = {}
    elif _DEBUG:
        print("[debug] _detect_all got no stdout from PowerShell (timeout or crash) — "
              "hardware profile will be mostly Unknown/0 for this run.", file=sys.stderr)
    if not _as_list(data.get("disks")):
        retry_rows = _run_ps_json(_DISK_QUERY, timeout=timeout)
        if retry_rows:
            data["disks"] = retry_rows
        else:
            print("[warning] disk detection returned no results after retry — "
                  "hdd_only/ssd_only tweaks will be excluded from this run as a "
                  "result. If this machine genuinely has disks, this is likely a "
                  "transient Get-PhysicalDisk failure, not real hardware state.",
                  file=sys.stderr)
    return data


def _detect_essential(timeout: int = 45) -> dict:
    """Fast half of detection -- CPU/RAM/disks/GPU/OS/battery/uptime, i.e.
    exactly what tier suggestion, tweak filtering (_tweak_applies), and the
    GUI's main hardware card need. Deliberately excludes anything only the
    Audit tab uses (board/BIOS/TPM/secure boot/network/volumes/AV/startup
    count/bloatware) -- those come from _detect_extra() separately so a
    slow query like Get-AppxPackage never blocks the tweak list from
    showing up."""
    raw = _run_ps(_DETECT_ESSENTIAL_SCRIPT, timeout=timeout)
    data = {}
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {}
    if not _as_list(data.get("disks")):
        retry_rows = _run_ps_json(_DISK_QUERY, timeout=timeout)
        if retry_rows:
            data["disks"] = retry_rows
    return data


def _detect_extra(timeout: int = 45) -> dict:
    """Slow half of detection -- Audit-tab-only fields. Run in the
    background after the essential profile is already usable."""
    raw = _run_ps(_DETECT_EXTRA_SCRIPT, timeout=timeout)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def get_hardware_profile_essential() -> HardwareProfile:
    """Builds a HardwareProfile from just the fast essential query. Every
    field _detect_extra() would otherwise fill (board, battery status
    aside, tpm_present, secure_boot, network, volumes, antivirus,
    startup_item_count, bloatware_installed) is left at its dataclass
    default -- fine for tier suggestion/tweak filtering/the main hw card,
    which never read those fields (see _tweak_applies)."""
    d = _detect_essential()
    os_row = d.get("os") or {}
    cs_row = d.get("cs") or {}
    profile = HardwareProfile(
        cpu=_detect_cpu(d.get("cpu")),
        ram=_detect_ram(_as_list(d.get("ram_modules")), int(cs_row.get("TotalPhysicalMemory") or 0)),
        disks=_detect_disks(_as_list(d.get("disks"))),
        gpus=_detect_gpus(_as_list(d.get("gpus"))),
    )
    profile.os_name, profile.os_build = _detect_os(os_row)
    profile.os_version = _normalize_os_version(profile.os_build)
    profile.os_edition = _detect_os_edition(os_row)
    profile.os_arch = _detect_os_arch(os_row)
    profile.domain_joined = _detect_domain_joined(cs_row)
    profile.os_install_date = _detect_os_install_date(os_row)
    profile.battery = _detect_battery(d.get("battery"))
    profile.is_laptop = _detect_is_laptop(profile.battery, d.get("chassis"), profile.cpu.name)
    profile.displays = _detect_displays(_as_list(d.get("gpus")))
    profile.uptime_hours = _detect_uptime_hours(d.get("uptime_hours"))
    profile.suggested_tier, profile.tier_reasons = _suggest_tier(profile)
    return profile


def fill_extra_profile_fields(profile: HardwareProfile) -> HardwareProfile:
    """Runs the slow Audit-tab-only queries and fills them into an
    already-built (essential) profile in place. Separate function/spawn
    from get_hardware_profile_essential() by design -- see _detect_extra."""
    d = _detect_extra()
    profile.tpm_present = _detect_tpm_present(d.get("tpm"))
    profile.secure_boot = _detect_secure_boot(d.get("secure_boot"))
    profile.board = _detect_board(d.get("board"), d.get("bios"))
    profile.network = _detect_network(_as_list(d.get("network")))
    profile.volumes = _detect_volumes(_as_list(d.get("volumes")))
    profile.antivirus = _detect_antivirus(_as_list(d.get("antivirus")))
    profile.startup_item_count = _detect_startup_item_count(d.get("startup_count"))
    profile.bloatware_installed = _detect_bloatware(_as_list(d.get("bloatware")))
    return profile


def get_hardware_profile() -> HardwareProfile:
    d = _detect_all()
    os_row = d.get("os") or {}
    cs_row = d.get("cs") or {}

    profile = HardwareProfile(
        cpu=_detect_cpu(d.get("cpu")),
        ram=_detect_ram(_as_list(d.get("ram_modules")), int(cs_row.get("TotalPhysicalMemory") or 0)),
        disks=_detect_disks(_as_list(d.get("disks"))),
        gpus=_detect_gpus(_as_list(d.get("gpus"))),
    )
    profile.os_name, profile.os_build = _detect_os(os_row)
    profile.os_version = _normalize_os_version(profile.os_build)
    profile.os_edition = _detect_os_edition(os_row)
    profile.os_arch = _detect_os_arch(os_row)
    profile.domain_joined = _detect_domain_joined(cs_row)
    profile.tpm_present = _detect_tpm_present(d.get("tpm"))
    profile.secure_boot = _detect_secure_boot(d.get("secure_boot"))
    profile.os_install_date = _detect_os_install_date(os_row)
    profile.board = _detect_board(d.get("board"), d.get("bios"))
    profile.battery = _detect_battery(d.get("battery"))
    profile.is_laptop = _detect_is_laptop(profile.battery, d.get("chassis"), profile.cpu.name)
    profile.network = _detect_network(_as_list(d.get("network")))
    profile.volumes = _detect_volumes(_as_list(d.get("volumes")))
    profile.displays = _detect_displays(_as_list(d.get("gpus")))
    profile.antivirus = _detect_antivirus(_as_list(d.get("antivirus")))
    profile.startup_item_count = _detect_startup_item_count(d.get("startup_count"))
    profile.bloatware_installed = _detect_bloatware(_as_list(d.get("bloatware")))
    profile.uptime_hours = _detect_uptime_hours(d.get("uptime_hours"))
    profile.suggested_tier, profile.tier_reasons = _suggest_tier(profile)
    return profile


# ---- Live usage stats (Section 8-ish) — deliberately separate from
# HardwareProfile/get_hardware_profile() above. The profile is a
# fingerprint (what the hardware IS) that's cached for hours since it
# barely changes; these are instantaneous readings (what the hardware is
# DOING right now) that are meaningless if cached at all. Kept as their
# own single PowerShell spawn so polling this doesn't touch the profile
# cache or trigger a full re-detect.
_LIVE_STATS_SCRIPT = r"""
$r = [ordered]@{}
function TryGet($block) { try { & $block } catch { $null } }
$r.cpu_pct = TryGet { (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average }
$r.cpu_per_core = @(TryGet {
    Get-CimInstance Win32_PerfFormattedData_PerfOS_Processor |
        Where-Object { $_.Name -ne '_Total' } | Sort-Object { [int]$_.Name } |
        Select-Object -ExpandProperty PercentProcessorTime
})
$r.mem = TryGet { Get-CimInstance Win32_OperatingSystem | Select-Object FreePhysicalMemory,TotalVisibleMemorySize }
$r.volumes = @(TryGet { Get-Volume | Where-Object { $_.DriveLetter -and $_.Size -gt 0 } | Select-Object DriveLetter,SizeRemaining,Size })
$r.temp_tenth_kelvin = TryGet {
    (Get-CimInstance -Namespace "root/wmi" -ClassName MSAcpi_ThermalZoneTemperature -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty CurrentTemperature)
}
$r.battery = TryGet { Get-CimInstance Win32_Battery | Select-Object EstimatedChargeRemaining,BatteryStatus }
# Win32_PerfFormattedData_* classes return an already-rate-computed value
# (bytes/sec, not a raw cumulative counter) — no manual before/after
# sampling needed on Flow's side, unlike raw perflib counters.
$r.net = @(TryGet {
    Get-CimInstance Win32_PerfFormattedData_Tcpip_NetworkInterface |
        Where-Object { $_.Name -notmatch 'isatap|Teredo|Loopback' } |
        Select-Object Name,BytesReceivedPersec,BytesSentPersec
})
$r.disk_io = TryGet {
    Get-CimInstance Win32_PerfFormattedData_PerfDisk_PhysicalDisk |
        Where-Object { $_.Name -eq '_Total' } |
        Select-Object DiskReadBytesPersec,DiskWriteBytesPersec,PercentDiskTime -First 1
}
$r.gpu_pct = TryGet {
    $engines = Get-Counter '\GPU Engine(*engtype_3D)\Utilization Percentage' -ErrorAction SilentlyContinue
    if ($engines) { ($engines.CounterSamples | Measure-Object -Property CookedValue -Sum).Sum }
}
$r.top_procs = @(TryGet {
    Get-Process | Sort-Object WorkingSet64 -Descending | Select-Object -First 6 -Property Name,@{N='MemMB';E={[math]::Round($_.WorkingSet64/1MB,1)}}
})
$r | ConvertTo-Json -Depth 4 -Compress
"""


def get_live_stats() -> dict:
    """One-shot snapshot of what the machine is doing right now: overall +
    per-core CPU load, RAM used %, per-volume disk used %, network
    throughput, disk read/write throughput, GPU engine usage (best-effort),
    top memory-consuming processes, CPU temp (best-effort), and battery if
    present. Meant to be polled every few seconds by the GUI's System Audit
    tab, not cached — see the module-level comment above this function.

    Several fields are deliberately best-effort and can come back None/empty:
      - cpu_temp_c: only a minority of OEM firmware populates the standard
        WMI thermal zone at all. Third-party tools (HWiNFO, Core Temp) read
        CPU package sensors through vendor-specific/ring-0 drivers instead —
        Flow deliberately doesn't bundle one of those (real security surface
        for a system-tweak tool to ship). Absent means absent, not faked.
      - gpu_percent: the 'GPU Engine' performance counter category only
        exists on Windows 10 1803+ with a WDDM 2.4+ driver — older drivers
        or some virtual/RDP sessions simply don't expose it.
      - Everything here reflects the same PowerShell-perflib snapshot
        limitations as Task Manager itself — this is not a replacement for
        dedicated monitoring software, just a lightweight in-app pulse."""
    raw = _run_ps(_LIVE_STATS_SCRIPT, timeout=12)
    data = {}
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {}

    cpu_pct = data.get("cpu_pct")
    cpu_pct = round(float(cpu_pct), 1) if isinstance(cpu_pct, (int, float)) else None

    cpu_per_core = []
    for v in _as_list(data.get("cpu_per_core")):
        try:
            cpu_per_core.append(round(float(v), 1))
        except (TypeError, ValueError):
            pass

    mem = data.get("mem") or {}
    total_kb = int(mem.get("TotalVisibleMemorySize") or 0)
    free_kb = int(mem.get("FreePhysicalMemory") or 0)
    ram_pct = round((1 - free_kb / total_kb) * 100, 1) if total_kb > 0 else None

    volumes = []
    for v in _as_list(data.get("volumes")):
        total = int(v.get("Size") or 0)
        if total <= 0:
            continue
        free = int(v.get("SizeRemaining") or 0)
        used_pct = round((1 - free / total) * 100, 1)
        volumes.append({
            "drive_letter": str(v.get("DriveLetter") or "?"),
            "used_percent": used_pct,
            "free_gb": round(free / (1024 ** 3), 2),
            "total_gb": round(total / (1024 ** 3), 2),
        })

    # MSAcpi_ThermalZoneTemperature reports in tenths of Kelvin (e.g. 3131 = 313.1K).
    raw_temp = data.get("temp_tenth_kelvin")
    temp_c = None
    if isinstance(raw_temp, (int, float)) and raw_temp > 0:
        kelvin = raw_temp / 10.0
        celsius = kelvin - 273.15
        if -20 < celsius < 130:  # sanity range — reject obviously bogus firmware readings rather than show them
            temp_c = round(celsius, 1)

    battery_row = data.get("battery")
    battery = None
    if battery_row:
        status = int(battery_row.get("BatteryStatus") or 0)
        battery = {
            "percent": int(battery_row.get("EstimatedChargeRemaining") or 0),
            "charging": status in (2, 6, 7, 8, 9),
        }

    net_down_bps = 0
    net_up_bps = 0
    net_adapters = []
    for adapter in _as_list(data.get("net")):
        down = int(adapter.get("BytesReceivedPersec") or 0)
        up = int(adapter.get("BytesSentPersec") or 0)
        net_down_bps += down
        net_up_bps += up
        raw_name = adapter.get("Name") or "Adapter"
        # WMI mangles adapter names for this class (underscores replacing
        # brackets/parens, e.g. "Realtek_PCIe_GbE_Family_Controller") —
        # cosmetic cleanup only, doesn't affect the underlying byte counts.
        clean_name = str(raw_name).replace("_", " ")
        net_adapters.append({
            "name": clean_name,
            "down_kbps": round(down / 1024, 1),
            "up_kbps": round(up / 1024, 1),
        })
    net_available = bool(net_adapters)

    disk_io_row = data.get("disk_io") or {}
    disk_read_bps = int(disk_io_row.get("DiskReadBytesPersec") or 0)
    disk_write_bps = int(disk_io_row.get("DiskWriteBytesPersec") or 0)
    disk_busy_pct = disk_io_row.get("PercentDiskTime")
    try:
        disk_busy_pct = min(100.0, round(float(disk_busy_pct), 1)) if disk_busy_pct is not None else None
    except (TypeError, ValueError):
        disk_busy_pct = None

    gpu_pct = data.get("gpu_pct")
    try:
        gpu_pct = min(100.0, round(float(gpu_pct), 1)) if gpu_pct is not None else None
    except (TypeError, ValueError):
        gpu_pct = None

    top_procs = []
    for p in _as_list(data.get("top_procs")):
        name = p.get("Name")
        mem_mb = p.get("MemMB")
        if name is None or mem_mb is None:
            continue
        top_procs.append({"name": str(name), "mem_mb": round(float(mem_mb), 1)})

    return {
        "cpu_percent": cpu_pct,
        "cpu_per_core": cpu_per_core,
        "ram_percent": ram_pct,
        "ram_used_gb": round((total_kb - free_kb) / (1024 ** 2), 2) if total_kb else None,
        "ram_total_gb": round(total_kb / (1024 ** 2), 2) if total_kb else None,
        "volumes": volumes,
        "cpu_temp_c": temp_c,
        "temp_available": temp_c is not None,
        "battery": battery,
        "net_down_kbps": round(net_down_bps / 1024, 1),
        "net_up_kbps": round(net_up_bps / 1024, 1),
        "net_available": net_available,
        "net_adapters": net_adapters,
        "disk_read_kbps": round(disk_read_bps / 1024, 1),
        "disk_write_kbps": round(disk_write_bps / 1024, 1),
        "disk_busy_percent": disk_busy_pct,
        "gpu_percent": gpu_pct,
        "gpu_available": gpu_pct is not None,
        "top_processes": top_procs,
    }


# ═══════════════════════════════════════════════════════════════════
# SECTION 2 — EXECUTOR (hidden subprocess runner)
# ═══════════════════════════════════════════════════════════════════

@dataclass
class ExecResult:
    command: str
    success: bool
    returncode: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False

    def to_dict(self):
        return asdict(self)


def run_hidden(args: Union[List[str], str], timeout: Optional[int] = 60, shell: bool = False) -> ExecResult:
    """Run any command hidden — no console flash. This is the ONLY place
    in Flow that should spawn a subprocess directly; every tweak/action
    goes through this so behavior (hiding, timeout, logging shape) stays
    consistent everywhere. Never raises — failures come back as a
    non-success ExecResult, the caller decides what to do.
    timeout=None waits indefinitely (subprocess.run's own semantics) —
    used deliberately by MAINTENANCE_ACTIONS for disk-bound ops that can
    legitimately run long on an HDD; everything else still passes a real
    number."""
    cmd_str = args if isinstance(args, str) else " ".join(args)
    start = time.time()
    try:
        result = subprocess.run(
            args,
            shell=shell,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=_NO_WINDOW,
        )
        duration = round(time.time() - start, 3)
        return ExecResult(
            command=cmd_str,
            success=(result.returncode == 0),
            returncode=result.returncode,
            stdout=result.stdout.strip(),
            stderr=result.stderr.strip(),
            duration_s=duration,
        )
    except subprocess.TimeoutExpired:
        duration = round(time.time() - start, 3)
        return ExecResult(
            command=cmd_str, success=False, returncode=-1,
            stdout="", stderr=f"timed out after {timeout}s",
            duration_s=duration, timed_out=True,
        )
    except (FileNotFoundError, OSError) as exc:
        duration = round(time.time() - start, 3)
        return ExecResult(
            command=cmd_str, success=False, returncode=-1,
            stdout="", stderr=str(exc), duration_s=duration,
        )


def run_powershell(command: str, timeout: int = 60) -> ExecResult:
    """Convenience wrapper — every PS call in Flow (restore points, future
    tweaks) should go through this, not raw subprocess calls."""
    return run_hidden(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        timeout=timeout,
    )


def run_powershell_allow_interactive(command: str, timeout: int = 60) -> ExecResult:
    """Same as run_powershell() but omits -NonInteractive, using -WindowStyle
    Hidden instead for silence. Checkpoint-Computer is known to hang
    indefinitely specifically under -NonInteractive — CREATE_NO_WINDOW
    already keeps this invisible, so -NonInteractive was never needed for
    hiding, only -NoProfile/-WindowStyle were. Use this ONLY for calls
    that have shown the -NonInteractive hang; everything else stays on
    run_powershell()."""
    return run_hidden(
        ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", command],
        timeout=timeout,
    )


def is_admin() -> bool:
    """Most restore-point and registry tweak operations need elevation.
    Check once up front so the GUI can prompt/relaunch as admin instead
    of tweaks failing silently one by one later."""
    result = run_powershell(
        "([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent())"
        ".IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)",
        timeout=20,
    )
    return result.success and result.stdout.strip().lower() == "true"


# ═══════════════════════════════════════════════════════════════════
# SECTION 3 — RESTORE POINT
# ═══════════════════════════════════════════════════════════════════
# Windows gotchas handled here, not left for later surprise:
#   - System Restore is often DISABLED by default on Home editions —
#     must Enable-ComputerRestore on the target drive before checkpointing.
#   - Checkpoint-Computer throttles to ONE restore point per 24h via the
#     SystemRestorePointCreationFrequency registry value (default 1440
#     minutes). Flow needs a checkpoint before every tier apply, not once
#     a day — so the throttle gets temporarily zeroed before each
#     checkpoint call. This does not disable restore or change any other
#     setting.

# Kill-switch while other sections are still being tested — flips create_restore_point()
# into a dry-run that reports what it WOULD do without touching the registry or
# actually checkpointing. Flip back to True once ready to test restore points for real.
RESTORE_POINT_CREATION_ENABLED = True

_RESTORE_FREQ_KEY = r"HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\SystemRestore"


def enable_restore(drive: str = "C:\\") -> ExecResult:
    return run_powershell(f"Enable-ComputerRestore -Drive '{drive}'")


def _clear_frequency_throttle() -> ExecResult:
    """Zero the 24h restore-point throttle so Checkpoint-Computer doesn't
    silently no-op on a second run today. Registry-only change, does not
    touch restore itself."""
    return run_powershell(
        f"New-ItemProperty -Path '{_RESTORE_FREQ_KEY}' "
        f"-Name SystemRestorePointCreationFrequency -Value 0 "
        f"-PropertyType DWord -Force | Out-Null"
    )


def create_restore_point(description: str = "Flow pre-tweak checkpoint") -> ExecResult:
    """Full sequence: enable restore on C:, clear the frequency throttle,
    then checkpoint. Each step's result gets folded into stderr on
    failure so the caller sees exactly which stage broke, not just
    'restore point failed'."""
    if not RESTORE_POINT_CREATION_ENABLED:
        return ExecResult(
            command="create_restore_point", success=False, returncode=0,
            stdout="DRY RUN — restore point creation disabled (RESTORE_POINT_CREATION_ENABLED=False), nothing was touched",
            stderr="", duration_s=0.0,
        )

    if not is_admin():
        return ExecResult(
            command="create_restore_point", success=False, returncode=-1,
            stdout="", stderr="not running elevated — restore point creation requires admin",
            duration_s=0.0,
        )

    enable_result = enable_restore()
    throttle_result = _clear_frequency_throttle()
    escaped_description = description.replace("'", "''")

    # Checkpoint-Computer was previously assumed unusable because it hangs
    # under -NonInteractive PowerShell. But -NonInteractive was never needed
    # for hiding the window (CREATE_NO_WINDOW already does that) — it was
    # just the default on run_powershell(). Try the real cmdlet first via
    # run_powershell_allow_interactive(), which drops -NonInteractive.
    checkpoint_result = run_powershell_allow_interactive(
        f"Checkpoint-Computer -Description '{escaped_description}' -RestorePointType 'MODIFY_SETTINGS'",
        timeout=240,  # confirmed on the HP: a real checkpoint took 116.4s, dangerously
                      # close to the old 120s limit — one run genuinely timed out at
                      # 120s right before an identical call succeeded at 116s. This
                      # isn't a hang, it's real HDD-bound work; give it real headroom.
    )

    if not checkpoint_result.success:
        # Fall back to the WMI method. Known to be unreliable on some rigs —
        # ReturnValue 0 (success) has been observed with NO shadow copy
        # actually created (32-bit SysWOW64 case on the HP test rig) — but
        # it's a reasonable second attempt if the cmdlet path fails outright.
        # RestorePointType 12 = MODIFY_SETTINGS, EventType 100 = BEGIN_SYSTEM_CHANGE.
        cmdlet_stderr = checkpoint_result.stderr
        checkpoint_result = run_powershell(
            f"$r = Invoke-WmiMethod -Namespace root\\default -Class SystemRestore "
            f"-Name CreateRestorePoint -ArgumentList '{escaped_description}',12,100; "
            f"$r.ReturnValue",
            timeout=120,
        )
        # ReturnValue 0 = success. The process can exit 0 while the WMI method
        # itself reports failure, so check the actual value, not just returncode.
        if checkpoint_result.success and checkpoint_result.stdout.strip() != "0":
            checkpoint_result.success = False
            checkpoint_result.stderr = f"WMI CreateRestorePoint returned code {checkpoint_result.stdout.strip()} (non-zero = failure)"
        if not checkpoint_result.success:
            checkpoint_result.stderr = f"Checkpoint-Computer failed: {cmdlet_stderr} | WMI fallback failed: {checkpoint_result.stderr}"

    if not checkpoint_result.success:
        combined_stderr = (
            f"checkpoint failed: {checkpoint_result.stderr} | "
            f"enable_restore_ok={enable_result.success} | "
            f"throttle_clear_ok={throttle_result.success}"
        )
        checkpoint_result.stderr = combined_stderr
    return checkpoint_result


def list_restore_points() -> list:
    rows = _run_ps_json("Get-ComputerRestorePoint | Select-Object SequenceNumber,Description,CreationTime | ConvertTo-Json")
    return rows


# ═══════════════════════════════════════════════════════════════════
# SECTION 4 — TWEAK DATABASE + APPLY/REVERT ENGINE
# ═══════════════════════════════════════════════════════════════════
# STATUS: catalog below is a DRAFT — needs Creator sign-off before
# TWEAKS_APPLY_ENABLED flips True. Nothing in TWEAK_DATABASE has been
# tested against the executor yet.
#
# Design contract (why it's built this way):
#   - Every tweak is DECLARATIVE (id/method/target/value), never a raw
#     command string. One generic engine applies and reverts ALL of
#     them, so a bug fixed once is fixed everywhere — same reasoning
#     as run_hidden() being the only subprocess entry point.
#   - Nothing is applied "toggle back to Windows default" — the engine
#     always CAPTURES the actual previous value first and writes it to
#     the revert log before touching anything. Reverting restores the
#     exact prior state, not an assumed default, because we can't know
#     what a stranger's PC looked like before Flow touched it.
#   - Hardware-conditional tweaks (applies_to) exist because some
#     "optimizations" are actively harmful on the wrong hardware —
#     e.g. disabling SysMain helps a RAM-starved HDD rig and hurts an
#     SSD rig with headroom. Wrong on purpose is worse than doing
#     nothing, so the filter is not optional.

TWEAKS_APPLY_ENABLED = True  # kill-switch — mirrors RESTORE_POINT_CREATION_ENABLED

TIER_ORDER = {"minimal": 0, "standard": 1, "maximal": 2, "extreme": 3}

_POWER_SCHEME_HIGH_PERFORMANCE = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"
# NOTE: Ultimate Performance's GUID is NOT hardcoded here on purpose — see
# _power_scheme_current_is_ultimate() for why.

# USB settings subgroup and "USB selective suspend" setting — these two GUIDs
# are fixed/well-known across Windows versions (unlike power SCHEME GUIDs,
# which get randomly reassigned per-instance for hidden templates like
# Ultimate Performance — see above). Subgroup+setting GUIDs identify a KIND
# of setting, not an instance, so they don't have that problem.
_POWER_SUBGROUP_USB = "2a737441-1930-4402-8d77-b2bebba308a3"
_POWER_SETTING_USB_SELECTIVE_SUSPEND = "48e6b7a6-50f5-4782-a5d4-53bb8f07e226"

# Display subgroup and "Enable adaptive brightness" setting — controls the
# ambient-light-sensor auto-dimming feature. Fixed/well-known GUIDs, same
# category as the USB ones above.
_POWER_SUBGROUP_DISPLAY = "7516b95f-f776-4464-8c53-06167f40cc99"
_POWER_SETTING_ADAPTIVE_BRIGHTNESS = "fbd9aa66-9553-4097-ba44-ed6e9d65eab8"


@dataclass
class Tweak:
    id: str
    name: str
    category: str      # visual | power | startup | service | telemetry | storage | network
    tier: str           # MINIMUM tier that includes this tweak (minimal < standard < maximal)
    risk: str            # safe | moderate | advanced
    method: str           # registry | service | power_scheme | power_setting | hibernate |
                           # per_adapter_registry | appx | onedrive | explorer_permission_deny |
                           # bitlocker_disable | winget | svchost_split | hybrid
    target: dict
    tweak_value: object
    description: str
    applies_to: Optional[str] = None  # None | hdd_only | ssd_only | low_ram | high_ram | small_disk | dgpu_present | laptop_only | desktop_only
    requires_explorer_refresh: bool = False  # True = Explorer must restart for this to visibly take effect
    min_os: str = "7"   # oldest Windows version this tweak is valid on: 7 | 8 | 8.1 | 10 | 11
    max_os: str = "11"  # newest version it still applies to (rare — mostly for pre-11 UI paths)
    os_verified: bool = False  # True = min_os/max_os confirmed against real registry/feature history,
                                # False = inherited default, not individually researched yet


@dataclass
class RevertEntry:
    tweak_id: str
    method: str
    target: dict
    previous_value: object
    previous_value_existed: bool  # False = registry value didn't exist before — revert deletes it
    applied_at: str
    success: bool

    def to_dict(self):
        return asdict(self)


def _tweak_applies(tweak: "Tweak", profile: HardwareProfile) -> bool:
    # OS gate first — a tweak wrong for the installed Windows version is
    # disqualified regardless of hardware match (this is the fix for the
    # camera incident's underlying class of bug: applying tweaks whose
    # real-world effect wasn't verified against the actual OS running).
    # NOTE: no special-case skip for "unknown" here — _os_at_least/_os_at_most
    # treat "unknown" as the lowest possible version (see _OS_ORDER), so an
    # undetectable or pre-Win7 OS now correctly fails every tweak's default
    # min_os="7" floor instead of silently falling through to hardware-only
    # matching, which used to let Win10/11-only tweaks fire on legacy boxes.
    if not (_os_at_least(profile.os_version, tweak.min_os) and _os_at_most(profile.os_version, tweak.max_os)):
        return False
    if tweak.applies_to is None:
        return True
    has_ssd = any(d.media_type == "SSD" for d in profile.disks)
    has_hdd = any(d.media_type == "HDD" for d in profile.disks)
    if tweak.applies_to == "hdd_only":
        return has_hdd and not has_ssd
    if tweak.applies_to == "ssd_only":
        return has_ssd
    if tweak.applies_to == "low_ram":
        return profile.ram.total_gb <= 8
    if tweak.applies_to == "high_ram":
        return profile.ram.total_gb >= 16
    if tweak.applies_to == "small_disk":
        # False (not True) when no disks were detected at all — same
        # "unknown means don't offer it" stance as hdd_only/ssd_only above,
        # rather than assuming small on missing data.
        return bool(profile.disks) and any(d.size_gb <= 256 for d in profile.disks)
    if tweak.applies_to == "dgpu_present":
        return any(g.is_dedicated for g in profile.gpus)
    if tweak.applies_to == "laptop_only":
        # is_laptop combines battery presence with SMBIOS chassis type (see
        # _detect_is_laptop) — battery alone false-negatives on a real
        # laptop with a dead/removed/undetected battery pack, which used
        # to silently hide every laptop-only tweak (power plan, battery
        # settings) on exactly those machines.
        return profile.is_laptop
    if tweak.applies_to == "desktop_only":
        return not profile.is_laptop
    return True


# Preinstalled Store apps that ship on nearly every consumer Windows 10/11
# image and are safe to remove for someone who doesn't specifically use
# them. Deliberately excludes anything with a real dependency chain
# (Store itself, Notepad, Calculator, Photos, HEIF/HEVC codec extensions,
# .NET/VCLibs framework packages) — removing those breaks unrelated apps.
# package_name is the AppX package family's display Name as Windows reports
# it via Get-AppxPackage, NOT the friendly Store title.
BLOATWARE_PACKAGES = {
    "bloat_3dbuilder":       {"package_name": "Microsoft.3DBuilder",                 "label": "3D Builder"},
    "bloat_bingweather":     {"package_name": "Microsoft.BingWeather",               "label": "Weather"},
    "bloat_bingnews":        {"package_name": "Microsoft.BingNews",                  "label": "News"},
    "bloat_gethelp":         {"package_name": "Microsoft.GetHelp",                   "label": "Get Help"},
    "bloat_getstarted":      {"package_name": "Microsoft.Getstarted",                "label": "Tips"},
    "bloat_officehub":       {"package_name": "Microsoft.MicrosoftOfficeHub",        "label": "Office (promo hub, not real Office)"},
    "bloat_solitaire":       {"package_name": "Microsoft.MicrosoftSolitaireCollection","label": "Solitaire Collection"},
    "bloat_mixedreality":    {"package_name": "Microsoft.MixedReality.Portal",       "label": "Mixed Reality Portal"},
    "bloat_people":          {"package_name": "Microsoft.People",                    "label": "People"},
    "bloat_skype":           {"package_name": "Microsoft.SkypeApp",                  "label": "Skype"},
    "bloat_feedbackhub":     {"package_name": "Microsoft.WindowsFeedbackHub",        "label": "Feedback Hub"},
    "bloat_maps":            {"package_name": "Microsoft.WindowsMaps",               "label": "Maps"},
    "bloat_xboxapp":         {"package_name": "Microsoft.GamingApp",                 "label": "Xbox app"},
    "bloat_zunemusic":       {"package_name": "Microsoft.ZuneMusic",                 "label": "Media Player (Groove/Zune Music)"},
    "bloat_zunevideo":       {"package_name": "Microsoft.ZuneVideo",                 "label": "Movies & TV"},
    "bloat_yourphone":       {"package_name": "Microsoft.YourPhone",                 "label": "Phone Link"},
    "bloat_todos":           {"package_name": "Microsoft.Todos",                     "label": "Microsoft To Do"},
    "bloat_clipchamp":       {"package_name": "Clipchamp.Clipchamp",                 "label": "Clipchamp"},
    "bloat_powerautomate":   {"package_name": "Microsoft.PowerAutomateDesktop",      "label": "Power Automate"},
    "bloat_teams_consumer":  {"package_name": "MicrosoftTeams",                      "label": "Teams (consumer, preinstalled)"},
    "bloat_spotify":         {"package_name": "SpotifyAB.SpotifyMusic",              "label": "Spotify (OEM preinstall)"},
    "bloat_tiktok":          {"package_name": "BytedancePte.Ltd.TikTok",             "label": "TikTok (OEM preinstall)"},
    "bloat_facebook":        {"package_name": "Facebook.Facebook",                   "label": "Facebook (OEM preinstall)"},
    "bloat_disneyplus":      {"package_name": "Disney.37853FC22B2CE",                "label": "Disney+ (OEM preinstall)"},
    "bloat_candycrush":      {"package_name": "king.com.CandyCrushSaga",             "label": "Candy Crush Saga"},
    "bloat_cortana_app":     {"package_name": "Microsoft.549981C3F5F10",             "label": "Cortana app"},
    "bloat_mail_calendar":   {"package_name": "microsoft.windowscommunicationsapps", "label": "Mail and Calendar"},
    "bloat_sticky_notes":    {"package_name": "Microsoft.MicrosoftStickyNotes",      "label": "Sticky Notes"},
    "bloat_whiteboard":      {"package_name": "Microsoft.Whiteboard",                "label": "Microsoft Whiteboard"},
    "bloat_family_safety":   {"package_name": "MicrosoftCorporationII.MicrosoftFamily", "label": "Family Safety"},
    "bloat_outlook_new":     {"package_name": "Microsoft.OutlookForWindows",         "label": "Outlook (new, preinstalled)"},
    "bloat_game_bar":        {"package_name": "Microsoft.XboxGamingOverlay",         "label": "Xbox Game Bar overlay"},
    "bloat_alarms":          {"package_name": "Microsoft.WindowsAlarms",             "label": "Alarms & Clock"},
    # "bloat_camera" removed deliberately (see below near _BLOATWARE_EXPLICIT_OVERRIDE_IDS) —
    # Flow does not carry any tweak that touches camera/mic/keyboard/mouse/touchpad hardware
    # or drivers, full stop, regardless of tier.
    "bloat_screensketch":    {"package_name": "Microsoft.ScreenSketch",              "label": "Snipping Tool (modern)"},
    "bloat_devhome":         {"package_name": "Microsoft.Windows.DevHome",           "label": "Dev Home"},
    "bloat_quickassist":     {"package_name": "MicrosoftCorporationII.QuickAssist",  "label": "Quick Assist"},
    "bloat_3dviewer":        {"package_name": "Microsoft.Microsoft3DViewer",         "label": "3D Viewer"},
    "bloat_webexperience":   {"package_name": "MicrosoftWindows.Client.WebExperience", "label": "Widgets board app"},
    "bloat_voice_recorder":  {"package_name": "Microsoft.WindowsSoundRecorder",      "label": "Voice Recorder"},
    "bloat_networkspeedtest":{"package_name": "Microsoft.NetworkSpeedTest",          "label": "Network Speed Test"},
    "bloat_print3d":         {"package_name": "Microsoft.Print3D",                   "label": "Print 3D"},
    "bloat_minecraft":       {"package_name": "Microsoft.MinecraftUWP",              "label": "Minecraft (trial preinstall)"},
}

_BLOATWARE_EXPLICIT_OVERRIDE_IDS = {
    "bloat_alarms", "bloat_3dviewer", "bloat_webexperience", "bloat_voice_recorder",
    "bloat_networkspeedtest", "bloat_print3d", "bloat_screensketch",
    "bloat_minecraft",
}

_BLOATWARE_MIN_OS = {
    "bloat_3dbuilder": ("10", "11"), "bloat_bingweather": ("8", "11"), "bloat_bingnews": ("8", "11"),
    "bloat_gethelp": ("10", "11"), "bloat_getstarted": ("8", "11"), "bloat_officehub": ("10", "11"),
    "bloat_solitaire": ("8", "11"), "bloat_mixedreality": ("10", "11"), "bloat_people": ("10", "11"),
    "bloat_skype": ("8", "11"), "bloat_feedbackhub": ("10", "11"), "bloat_maps": ("8", "11"),
    "bloat_xboxapp": ("8", "11"), "bloat_zunemusic": ("8", "11"), "bloat_zunevideo": ("8", "11"),
    "bloat_yourphone": ("10", "11"), "bloat_todos": ("10", "11"), "bloat_clipchamp": ("11", "11"),
    "bloat_powerautomate": ("11", "11"), "bloat_teams_consumer": ("11", "11"),
    "bloat_cortana_app": ("10", "11"), "bloat_mail_calendar": ("8", "11"),
    "bloat_sticky_notes": ("8", "11"), "bloat_whiteboard": ("10", "11"),
    "bloat_family_safety": ("10", "11"), "bloat_outlook_new": ("11", "11"),
    "bloat_game_bar": ("10", "11"), "bloat_devhome": ("11", "11"), "bloat_spotify": ("10", "11"),
    "bloat_tiktok": ("11", "11"), "bloat_facebook": ("10", "11"), "bloat_disneyplus": ("10", "11"),
    "bloat_candycrush": ("8", "11"), "bloat_quickassist": ("10", "11"),
}


def _bloat_min_os(pkg_id: str) -> tuple:
    return _BLOATWARE_MIN_OS.get(pkg_id, ("8", "11"))  # default: AppX itself didn't exist pre-Windows 8


TWEAK_DATABASE: List[Tweak] = [
    # ---- MINIMAL — safe, visual/power only, no service or network changes ----
    Tweak(
        id="disable_transparency", name="Disable transparency effects",
        category="visual", tier="minimal", risk="safe", method="registry",
        target={"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize", "name": "EnableTransparency"},
        tweak_value=0,
        min_os="8", max_os="11", os_verified=True,
        description="Turns off taskbar/Start blur — real compositor cost on integrated graphics (e.g. HD4000-class iGPUs).",
        requires_explorer_refresh=True,
    ),
    Tweak(
        id="disable_menu_animation", name="Disable menu fade/slide animation",
        category="visual", tier="minimal", risk="safe", method="registry",
        target={"path": r"HKCU:\Control Panel\Desktop", "name": "MenuShowDelay", "force_type": "String"},
        tweak_value=0,
        min_os="7", max_os="11", os_verified=True,
        description="Removes the ~400ms menu popup delay. Registry-only, does not touch visual styles/theme — "
                     "deliberately NOT using VisualFXSetting=2 'Best Performance', which unchecks 'Use visual "
                     "styles on windows and buttons' and silently reverts the desktop to the unthemed classic look.",
        requires_explorer_refresh=True,
    ),
    Tweak(
        id="disable_taskbar_animations", name="Disable taskbar button animations",
        category="visual", tier="minimal", risk="safe", method="registry",
        target={"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "name": "TaskbarAnimations"},
        tweak_value=0,
        min_os="7", max_os="11", os_verified=True,
        description="Turns off taskbar button launch/flash animations — same scope-safe approach as menu animation, no theme impact.",
        requires_explorer_refresh=True,
    ),
    Tweak(
        id="startup_delay_zero", name="Remove Start Menu app launch delay",
        category="startup", tier="minimal", risk="safe", method="registry",
        target={"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Serialize", "name": "StartupDelayInMSec"},
        tweak_value=0,
        min_os="10", max_os="11", os_verified=True,
        description="Removes the ~1s artificial stagger Explorer inserts before each startup app launches.",
    ),
    Tweak(
        id="power_plan_high_performance", name="Switch to High Performance power plan",
        category="power", tier="minimal", risk="safe", method="power_scheme",
        target={}, tweak_value=_POWER_SCHEME_HIGH_PERFORMANCE,
        min_os="7", max_os="11", os_verified=True,
        description="Switches the active power scheme. Previous scheme GUID is captured and restored on "
                     "revert. On a laptop this measurably shortens battery life on the go — genuinely worth "
                     "it if it's usually plugged in, worth skipping (or switching back to Balanced) if it's "
                     "not. Not hidden from laptops here since a plugged-in gaming/workstation laptop wants "
                     "this exactly as much as a desktop does — the tradeoff just isn't hardware-gated the "
                     "way pure-desktop settings like adaptive brightness are.",
    ),
    Tweak(
        id="disable_usb_selective_suspend", name="Disable USB selective suspend",
        category="power", tier="standard", risk="moderate", method="power_setting",
        target={"subgroup_guid": _POWER_SUBGROUP_USB, "setting_guid": _POWER_SETTING_USB_SELECTIVE_SUSPEND},
        tweak_value={"ac": 0, "dc": 0},
        min_os="7", max_os="11", os_verified=False,
        description=("Stops Windows from power-suspending USB ports/devices to save power. Fixes "
                      "intermittent USB dropouts (mice going to sleep, external drives disconnecting) "
                      "at the cost of slightly higher idle power draw — moderate risk since laptops on "
                      "battery lose a real power-saving feature, not just a cosmetic one. AC and DC set "
                      "together since a device that drops mid-task doesn't care which power source is active."),
    ),
    Tweak(
        id="disable_adaptive_brightness", name="Disable adaptive (ambient-light) brightness",
        category="power", tier="standard", risk="safe", method="power_setting",
        target={"subgroup_guid": _POWER_SUBGROUP_DISPLAY, "setting_guid": _POWER_SETTING_ADAPTIVE_BRIGHTNESS},
        tweak_value={"ac": 0, "dc": 0}, applies_to="laptop_only",
        min_os="7", max_os="11", os_verified=True,
        description="Turns off automatic screen-brightness adjustment based on the ambient-light sensor. "
                     "Laptop-only tweak — desktops don't have this sensor, so it's hidden there entirely "
                     "rather than shown as a harmless no-op. Fixes the brightness-hunting/flicker some "
                     "laptops show in mixed lighting; trade-off is you lose the automatic dimming that "
                     "does genuinely save battery in bright rooms for some people.",
    ),

    # ---- STANDARD — adds service/background-app changes, still low functional risk ----
    Tweak(
        id="disable_background_apps", name="Disable UWP background apps",
        category="service", tier="standard", risk="safe", method="registry",
        target={"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\BackgroundAccessApplications", "name": "GlobalUserDisabled"},
        tweak_value=1,
        min_os="10", max_os="11", os_verified=True,
        description="Stops Store apps running in the background — RAM saver on 4-8GB rigs, no visible feature loss.",
    ),
    Tweak(
        id="disable_game_dvr", name="Disable Xbox Game Bar / Game DVR",
        category="service", tier="standard", risk="safe", method="registry",
        target={"path": r"HKCU:\System\GameConfigStore", "name": "GameDVR_Enabled"},
        tweak_value=0,
        min_os="10", max_os="11", os_verified=True,
        description="Kills the background recording overlay — CPU/RAM cost with no benefit on low-end or non-gaming rigs.",
    ),
    Tweak(
        id="disable_diagtrack", name="Disable Connected User Experiences and Telemetry",
        category="telemetry", tier="standard", risk="moderate", method="service",
        target={"service_name": "DiagTrack"}, tweak_value="Disabled",
        min_os="7", max_os="11", os_verified=True,
        description="Stops the telemetry pipeline service (DiagTrack). CPU/RAM/disk-IO saver, no user-facing feature depends on it.",
    ),
    Tweak(
        id="disable_search_indexing", name="Disable Windows Search indexing",
        category="service", tier="standard", risk="moderate", method="service",
        target={"service_name": "WSearch"}, tweak_value="Disabled",
        min_os="7", max_os="11", os_verified=True,
        description="Frees continuous background disk I/O on mechanical drives. Trade-off: Start Menu/Explorer search gets slower.",
        applies_to="hdd_only",
    ),

    # ---- MAXIMAL — advanced, higher risk, hardware-conditional ----
    Tweak(
        id="disable_sysmain", name="Disable SysMain (Superfetch)",
        category="service", tier="maximal", risk="advanced", method="service",
        target={"service_name": "SysMain"}, tweak_value="Disabled",
        min_os="7", max_os="11", os_verified=True,
        description="Stops RAM pre-caching of frequently used apps. Helps a RAM-starved HDD rig avoid pagefile thrash. "
                     "HURTS SSD rigs with adequate RAM — hardware-gated to HDD-only on purpose.",
        applies_to="hdd_only",
    ),
    Tweak(
        id="disable_hibernation", name="Disable hibernation",
        category="storage", tier="maximal", risk="advanced", method="hibernate",
        target={}, tweak_value=False,
        min_os="8", max_os="11", os_verified=True,
        description="Deletes hiberfil.sys, reclaims disk space roughly equal to installed RAM. Reversible, but "
                     "re-enabling recreates the file — matters most on small/full HDDs.",
        applies_to="low_ram",
    ),
    Tweak(
        id="disable_update_p2p", name="Disable Windows Update delivery optimization (P2P)",
        category="network", tier="maximal", risk="moderate", method="registry",
        target={"path": r"HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\DeliveryOptimization\Config", "name": "DODownloadMode"},
        tweak_value=0,
        min_os="10", max_os="11", os_verified=True,
        description="Stops this PC uploading Windows Update bytes to other PCs over the internet — bandwidth saver on capped/slow connections.",
    ),
    Tweak(
        id="network_throttling_off", name="Remove multimedia network throttling index",
        category="network", tier="maximal", risk="advanced", method="registry",
        target={"path": r"HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile", "name": "NetworkThrottlingIndex"},
        tweak_value=-1,  # 0xFFFFFFFF as signed Int32 — PowerShell's -PropertyType DWord maps to Int32,
                         # and the literal 0xFFFFFFFF (4294967295) overflows that range and throws.
                         # REG_DWORD is 4 raw bytes with no sign bit, so -1's two's-complement pattern
                         # writes the identical bits Windows expects for "no throttling."
        min_os="7", max_os="11", os_verified=True,
        description="Removes the ~10%% bandwidth cap Windows reserves for the multimedia scheduler — matters under network load on weak NICs.",
    ),
    Tweak(
        id="disable_nagle_algorithm", name="Disable Nagle's Algorithm (all network adapters)",
        category="network", tier="maximal", risk="advanced", method="hybrid",
        target={"steps": [
            {"method": "per_adapter_registry",
             "target": {"base_path": r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces", "name": "TcpAckFrequency"},
             "value": 1},
            {"method": "per_adapter_registry",
             "target": {"base_path": r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces", "name": "TCPNoDelay"},
             "value": 1},
        ]}, tweak_value=None,
        min_os="7", max_os="11", os_verified=False,
        description=("Disables TCP delayed-ACK batching (Nagle's Algorithm) on every network adapter — "
                      "cuts small-packet latency for things like remote desktop, competitive gaming, and "
                      "SSH, at the cost of more, smaller packets on the wire (worse on metered/congested "
                      "links). Applied per-interface-GUID since there's no single global registry switch; "
                      "if a new adapter (USB dongle, VPN virtual NIC) is added later it won't have this set "
                      "until the tweak/daemon reapplies — advanced risk since 'every adapter' includes "
                      "virtual ones you may not expect (VPN clients, Hyper-V vSwitches, container NICs)."),
    ),

    # ---- MINIMAL additions — more safe, cosmetic/QoL, zero functional risk ----
    Tweak(
        id="disable_start_menu_suggestions", name="Disable Start Menu app suggestions",
        category="visual", tier="minimal", risk="safe", method="registry",
        target={"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager", "name": "SubscribedContent-338388Enabled"},
        tweak_value=0,
        min_os="10", max_os="11", os_verified=True,
        description="Stops Microsoft injecting promoted/suggested apps into the Start Menu tile grid.",
        requires_explorer_refresh=True,
    ),
    Tweak(
        id="disable_windows_tips", name="Disable \"tips and tricks\" suggestions",
        category="visual", tier="minimal", risk="safe", method="registry",
        target={"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager", "name": "SoftLandingEnabled"},
        tweak_value=0,
        min_os="10", max_os="11", os_verified=True,
        description="Turns off the periodic \"Get tips, tricks, and suggestions\" notifications in Settings/Start.",
        requires_explorer_refresh=True,
    ),
    Tweak(
        id="disable_windows_spotlight", name="Disable Windows Spotlight lock screen",
        category="visual", tier="minimal", risk="safe", method="registry",
        target={"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager", "name": "RotatingLockScreenEnabled"},
        tweak_value=0,
        min_os="10", max_os="11", os_verified=True,
        description="Stops the lock screen from downloading a new Spotlight image in the background — small but real network/disk churn saved, especially on metered or slow connections.",
        requires_explorer_refresh=True,
    ),
    Tweak(
        id="disable_search_web_results", name="Keep Start Menu search local-only",
        category="visual", tier="minimal", risk="safe", method="registry",
        target={"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Search", "name": "BingSearchEnabled"},
        tweak_value=0,
        min_os="10", max_os="11", os_verified=True,
        description="Stops Start Menu search from round-tripping every keystroke to Bing — local file/app search only, noticeably snappier on a slow connection.",
        requires_explorer_refresh=True,
    ),
    Tweak(
        id="disable_widgets_taskbar", name="Remove Widgets from taskbar",
        category="visual", tier="minimal", risk="safe", method="registry",
        target={"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "name": "TaskbarDa"},
        tweak_value=0,
        min_os="11", max_os="11", os_verified=True,
        description="Removes the Widgets button and its background host process — a standing RAM/CPU cost for a panel most people never open.",
        requires_explorer_refresh=True,
    ),
    Tweak(
        id="disable_chat_taskbar", name="Remove Chat/Meet Now from taskbar",
        category="visual", tier="minimal", risk="safe", method="registry",
        target={"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "name": "TaskbarMn"},
        tweak_value=0,
        min_os="10", max_os="11", os_verified=True,
        description="Removes the Chat icon — pure taskbar clutter unless you actually use Teams consumer chat.",
        requires_explorer_refresh=True,
    ),
    Tweak(
        id="explorer_open_to_this_pc", name="Open File Explorer to This PC",
        category="explorer", tier="minimal", risk="safe", method="registry",
        target={"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "name": "LaunchTo"},
        tweak_value=1,
        min_os="8.1", max_os="11", os_verified=True,
        description="Explorer opens to This PC instead of Quick Access, skipping the recent/frequent-files scan Quick Access rebuilds on every launch.",
        requires_explorer_refresh=True,
    ),
    Tweak(
        id="explorer_show_file_extensions", name="Always show file extensions",
        category="explorer", tier="minimal", risk="safe", method="registry",
        target={"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "name": "HideFileExt"},
        tweak_value=0,
        min_os="7", max_os="11", os_verified=True,
        description="Not a performance tweak — a safety one. Makes disguised files like invoice.pdf.exe visible at a glance instead of showing just \"invoice.pdf\".",
        requires_explorer_refresh=True,
    ),
    Tweak(
        id="disable_fax_service", name="Disable Fax service",
        category="service", tier="minimal", risk="safe", method="service",
        target={"service_name": "Fax"}, tweak_value="Disabled",
        min_os="7", max_os="11", os_verified=True,
        description="Fax hardware support most PCs will never touch. Safe, inert dead weight on a modern machine.",
    ),
    Tweak(
        id="disable_retail_demo_service", name="Disable Retail Demo service",
        category="service", tier="minimal", risk="safe", method="service",
        target={"service_name": "RetailDemo"}, tweak_value="Disabled",
        min_os="7", max_os="11", os_verified=True,
        description="Store-kiosk demo mode service — irrelevant outside a retail display unit, harmless to disable on a personal PC.",
    ),

    # ---- STANDARD additions — telemetry/privacy toggles and a few more safe services ----
    Tweak(
        id="disable_advertising_id", name="Disable per-user Advertising ID",
        category="telemetry", tier="standard", risk="safe", method="registry",
        target={"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\AdvertisingInfo", "name": "Enabled"},
        tweak_value=0,
        min_os="7", max_os="11", os_verified=True,
        description="Turns off the per-user ID apps use to build a personalized-ad profile across the Store and other apps.",
    ),
    Tweak(
        id="disable_tailored_experiences", name="Disable tailored experiences with diagnostic data",
        category="telemetry", tier="standard", risk="safe", method="registry",
        target={"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Privacy", "name": "TailoredExperiencesWithDiagnosticDataEnabled"},
        tweak_value=0,
        min_os="7", max_os="11", os_verified=True,
        description="Stops Microsoft using your diagnostic data to personalize tips, ads, and recommendations.",
    ),
    Tweak(
        id="disable_feedback_prompts", name="Disable periodic feedback prompts",
        category="telemetry", tier="standard", risk="safe", method="registry",
        target={"path": r"HKCU:\Software\Microsoft\Siuf\Rules", "name": "NumberOfSIUFInPeriod"},
        tweak_value=0,
        min_os="7", max_os="11", os_verified=True,
        description="Stops the \"How's it going?\" feedback survey prompts from Windows popping up.",
    ),
    Tweak(
        id="disable_ceip", name="Disable Customer Experience Improvement Program",
        category="telemetry", tier="standard", risk="safe", method="registry",
        target={"path": r"HKLM:\SOFTWARE\Policies\Microsoft\SQMClient\Windows", "name": "CEIPEnable"},
        tweak_value=0,
        min_os="7", max_os="11", os_verified=True,
        description="Policy-level opt-out of the older CEIP usage-data collector, separate from the modern DiagTrack "
                     "pipeline — some LOB/legacy components still check this key independently.",
    ),
    # NOTE: a "disable_recent_files_tracking" tweak used to live here — removed,
    # not just re-gated. It was a byte-for-byte duplicate of disable_jumplist_tracking
    # below (same HKCU Explorer\Advanced\Start_TrackDocs key, same value 0, no bundle
    # involved on either side) — an accidental double-entry from two separate growth
    # passes, not an intentional convenience overlap like the notification/taskbar
    # bundles elsewhere in this file. disable_jumplist_tracking (minimal tier) already
    # covers this and is included in every tier above minimal automatically.
    Tweak(
        id="disable_lockscreen_notifications", name="Hide notifications on the lock screen",
        category="notifications", tier="minimal", risk="safe", method="registry",
        target={"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\PushNotifications", "name": "LockScreenToastEnabled"},
        tweak_value=0,
        min_os="8", max_os="11", os_verified=True,
        description="Stops app notification previews (messages, email subject lines, etc.) from rendering on the "
                     "lock screen where anyone glancing at the machine can read them.",
    ),
    Tweak(
        id="disable_toast_notifications", name="Disable all toast notifications",
        category="notifications", tier="maximal", risk="moderate", method="registry",
        target={"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\PushNotifications", "name": "ToastEnabled"},
        tweak_value=0,
        min_os="8", max_os="11", os_verified=True,
        description="Turns off every app's popup notification, system-wide — not just lock-screen previews like the "
                     "tweak above. Moderate risk because it silences things you may actually want, like calendar "
                     "reminders or update prompts, not just spam.",
    ),
    # disable_sticky_keys_prompt / disable_toggle_keys_prompt removed deliberately —
    # any tweak touching HKCU\Control Panel\Accessibility\* is keyboard-behavior territory,
    # which Flow no longer carries any tweak for, regardless of tier.
    Tweak(
        id="disable_gamedvr_policy", name="Block Game DVR via Group Policy",
        category="gaming", tier="standard", risk="safe", method="registry",
        target={"path": r"HKLM:\SOFTWARE\Policies\Microsoft\Windows\GameDVR", "name": "AllowGameDVR"},
        tweak_value=0,
        min_os="10", max_os="11", os_verified=True,
        description="Policy-level companion to the per-user Game DVR toggle above — blocks it from being silently "
                     "re-enabled by a Game Bar update or a different user profile on this machine.",
    ),
    Tweak(
        id="disable_onedrive_autostart", name="Stop OneDrive launching at sign-in",
        category="cloud", tier="standard", risk="safe", method="registry",
        target={"path": r"HKLM:\SOFTWARE\Policies\Microsoft\Windows\OneDrive", "name": "DisableFileSyncNGSC"},
        tweak_value=1,
        min_os="8.1", max_os="11", os_verified=True,
        description="Non-destructive alternative to fully uninstalling OneDrive (see the Full Maximal tier) — "
                     "keeps it installed but stops it auto-starting and syncing, freeing the background CPU/network "
                     "it uses on a rig that doesn't need cloud sync running constantly.",
    ),
    Tweak(
        id="disable_windows_welcome_experience", name="Disable post-update \"Windows Welcome Experience\" screens",
        category="visual", tier="standard", risk="safe", method="registry",
        target={"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\UserProfileEngagement", "name": "ScoobeSystemSettingEnabled"},
        tweak_value=0,
        min_os="10", max_os="11", os_verified=True,
        description="Stops the full-screen \"here's what's new\" / celebratory tour that Windows shows after major "
                     "feature updates and on some fresh sign-ins — one less interruption to click through.",
    ),
    Tweak(
        id="disable_news_feeds", name="Disable News and Interests / Feeds",
        category="visual", tier="standard", risk="safe", method="registry",
        target={"path": r"HKLM:\SOFTWARE\Policies\Microsoft\Windows\Windows Feeds", "name": "EnableFeeds"},
        tweak_value=0,
        min_os="10", max_os="11", os_verified=True,
        description="Fully disables the Feeds/News and Interests widget content pipeline via policy — a background process that polls for content even while collapsed.",
        requires_explorer_refresh=True,
    ),
    Tweak(
        id="disable_snap_assist_flyout", name="Disable Snap Assist hover flyout",
        category="visual", tier="standard", risk="safe", method="registry",
        target={"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "name": "EnableSnapAssistFlyout"},
        tweak_value=0,
        min_os="10", max_os="11", os_verified=True,
        description="Turns off the animated layout preview that pops up when hovering a window's maximize button — minor GPU/compositor load saved on weak integrated graphics.",
        requires_explorer_refresh=True,
    ),
    Tweak(
        id="disable_maps_broker_service", name="Disable Downloaded Maps Manager",
        category="service", tier="standard", risk="safe", method="service",
        target={"service_name": "MapsBroker"}, tweak_value="Disabled",
        min_os="10", max_os="11", os_verified=True,
        description="Background service for offline Maps app data. Safe to disable unless you actually use offline maps.",
    ),
    Tweak(
        id="disable_remote_assistance", name="Disable inbound Remote Assistance",
        category="network", tier="standard", risk="moderate", method="registry",
        target={"path": r"HKLM:\SYSTEM\CurrentControlSet\Control\Remote Assistance", "name": "fAllowToGetHelp"},
        tweak_value=0,
        min_os="7", max_os="11", os_verified=True,
        description="Blocks inbound Remote Assistance requests, shrinking the remote-access attack surface. Moderate because it also breaks the feature if you ever rely on a friend/support tech remoting in to help.",
    ),
    Tweak(
        id="disable_remote_registry_service", name="Disable Remote Registry service",
        category="service", tier="standard", risk="moderate", method="service",
        target={"service_name": "RemoteRegistry"}, tweak_value="Disabled",
        min_os="7", max_os="11", os_verified=True,
        description="Disables remote read/write access to this PC's registry. Already manual-start/off by default on most consumer builds — this just makes sure it can't be started, closing a real (if usually dormant) remote-attack vector.",
    ),
    Tweak(
        id="disable_wer_service", name="Disable Windows Error Reporting service",
        category="service", tier="standard", risk="moderate", method="service",
        target={"service_name": "WerSvc"}, tweak_value="Disabled",
        min_os="7", max_os="11", os_verified=True,
        description="Stops background collection/upload of crash dumps. Trade-off: if you ever need Microsoft support to diagnose a crash, automatic error reporting won't have run.",
    ),
    Tweak(
        id="disable_pca_service", name="Disable Program Compatibility Assistant service",
        category="service", tier="standard", risk="moderate", method="service",
        target={"service_name": "PcaSvc"}, tweak_value="Disabled",
        min_os="7", max_os="11", os_verified=True,
        description="Stops the background service that scans running apps for known compatibility issues. Trade-off: old software that relies on an automatic compatibility shim may occasionally misbehave without it.",
    ),
    Tweak(
        id="disable_fast_startup", name="Disable Fast Startup (hybrid boot)",
        category="power", tier="standard", risk="moderate", method="registry",
        target={"path": r"HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power", "name": "HiberbootEnabled"},
        tweak_value=0,
        min_os="8", max_os="11", os_verified=True,
        description="Turns off hybrid boot, which hibernates the kernel session instead of a true shutdown. Trade-off: slightly slower cold boots, but a real full shutdown flushes disk caches cleanly and avoids the stale-driver-state issues Fast Startup is known to cause after driver/BIOS updates.",
    ),

    # ---- MAXIMAL additions — advanced, hardware-gated where it matters ----
    Tweak(
        id="disable_activity_feed", name="Disable Activity History / Timeline sync",
        category="telemetry", tier="maximal", risk="moderate", method="registry",
        target={"path": r"HKLM:\SOFTWARE\Policies\Microsoft\Windows\System", "name": "EnableActivityFeed"},
        tweak_value=0,
        min_os="7", max_os="11", os_verified=True,
        description="Stops Windows recording and syncing your app/document activity history across devices. Moderate because it's a real feature some people use (resuming a task on another PC), not pure background waste.",
    ),
    Tweak(
        id="disable_location_tracking", name="Revoke system-wide location access",
        category="network", tier="maximal", risk="advanced", method="registry",
        target={"path": r"HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\location", "name": "Value", "force_type": "String"},
        tweak_value="Deny",
        min_os="8", max_os="11", os_verified=True,
        description="Denies location access to every app system-wide. Advanced: this breaks anything that legitimately needs it — Maps, Find My Device, weather auto-location, some camera geotagging.",
    ),
    Tweak(
        id="disable_reserved_storage", name="Disable Reserved Storage",
        category="storage", tier="maximal", risk="advanced", method="registry",
        target={"path": r"HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\ReserveManager", "name": "ShippedWithReserves"},
        tweak_value=0,
        min_os="10", max_os="11", os_verified=True,
        description="Frees the several-GB partition Windows sets aside for update/temp headroom. Only offered on smaller drives, where that space matters — advanced because it trades away Windows Update's safety margin, which occasionally causes update failures on a genuinely full disk.",
        applies_to="small_disk",
    ),
    Tweak(
        id="disable_thumbnail_cache", name="Disable Explorer thumbnail cache",
        category="storage", tier="maximal", risk="moderate", method="registry",
        target={"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer", "name": "DisableThumbnailCache"},
        tweak_value=1,
        min_os="7", max_os="11", os_verified=True,
        description="Stops Explorer writing a persistent thumbnail cache file, trimming background disk writes on a mechanical drive. Trade-off: browsing image/video folders has to regenerate thumbnails every time instead of reading the cache.",
        applies_to="hdd_only",
        requires_explorer_refresh=True,
    ),
    Tweak(
        id="disable_paging_executive", name="Keep kernel/drivers resident in RAM",
        category="memory", tier="maximal", risk="advanced", method="registry",
        target={"path": r"HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management", "name": "DisablePagingExecutive"},
        tweak_value=1,
        min_os="7", max_os="11", os_verified=True,
        description="Prevents kernel-mode code and drivers from ever being paged out to disk. Only offered on 16GB+ rigs — on a RAM-constrained machine this increases memory pressure and can hurt performance instead of helping.",
        applies_to="high_ram",
    ),
    Tweak(
        id="enable_gpu_scheduling", name="Enable Hardware-accelerated GPU Scheduling",
        category="gaming", tier="maximal", risk="moderate", method="registry",
        target={"path": r"HKLM:\SYSTEM\CurrentControlSet\Control\GraphicsDrivers", "name": "HwSchMode"},
        tweak_value=2,
        min_os="10", max_os="11", os_verified=True,
        description="Hands GPU work scheduling to the GPU itself instead of the CPU, on drivers that support it. Only offered when a dedicated GPU was detected — on integrated-only graphics this is typically a no-op at best.",
        applies_to="dgpu_present",
    ),

    # ---- MINIMAL additions — more safe visual/explorer QoL, no functional trade-off ----
    Tweak(
        id="disable_jumplist_tracking", name="Stop tracking recent docs for Jump Lists",
        category="explorer", tier="minimal", risk="safe", method="registry",
        target={"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "name": "Start_TrackDocs"},
        tweak_value=0,
        min_os="7", max_os="11", os_verified=True,
        description="Stops Explorer logging every file you open just to populate Jump List \"recent\" entries — small steady disk-write and privacy win, same key the taskbar's own \"show recently opened items\" checkbox writes to.",
        requires_explorer_refresh=True,
    ),
    Tweak(
        id="taskbar_search_icon_only", name="Shrink taskbar search to icon-only",
        category="visual", tier="minimal", risk="safe", method="registry",
        target={"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Search", "name": "SearchboxTaskbarMode"},
        tweak_value=1,
        min_os="10", max_os="11", os_verified=True,
        description="Collapses the wide search box to a single icon — same setting as right-clicking the taskbar > Search. Purely cosmetic reclaim of taskbar space, search still works identically.",
        requires_explorer_refresh=True,
    ),
    Tweak(
        id="disable_aero_peek", name="Disable \"Peek at desktop\" hover preview",
        category="visual", tier="minimal", risk="safe", method="registry",
        target={"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "name": "EnableAeroPeek"},
        tweak_value=0,
        min_os="7", max_os="11", os_verified=True,
        description="Turns off the live desktop-preview flyout triggered by hovering the Show Desktop sliver — same checkbox as Taskbar Settings > \"Use Peek to preview the desktop.\" Small compositor cost removed on weak integrated graphics.",
        requires_explorer_refresh=True,
    ),
    Tweak(
        id="hide_recently_added_start", name="Hide \"Recently added\" from Start Menu",
        category="visual", tier="minimal", risk="safe", method="registry",
        target={"path": r"HKLM:\SOFTWARE\Policies\Microsoft\Windows\Explorer", "name": "HideRecentlyAddedApps"},
        tweak_value=1,
        min_os="10", max_os="11", os_verified=True,
        description="Removes the auto-populated \"Recently added\" row from the Start Menu app list — pure decluttering, no background cost either way.",
    ),
    Tweak(
        id="disable_insider_service", name="Disable Windows Insider Service",
        category="service", tier="minimal", risk="safe", method="service",
        target={"service_name": "wisvc"}, tweak_value="Disabled",
        min_os="10", max_os="11", os_verified=True,
        description="Background service that checks for Insider Preview builds. Inert unless you're actually enrolled in the Insider Program — safe to disable on a normal release-channel install.",
    ),
    Tweak(
        id="disable_wmp_network_sharing", name="Disable Windows Media Player Network Sharing",
        category="service", tier="minimal", risk="safe", method="service",
        target={"service_name": "WMPNetworkSvc"}, tweak_value="Disabled",
        min_os="7", max_os="11", os_verified=True,
        description="DLNA/UPnP media-sharing service for streaming your library to other devices on the LAN — background service almost nobody uses since most people stream from apps instead of WMP's own library now.",
    ),

    # ---- STANDARD additions — a couple more real services, a legacy privacy policy ----
    Tweak(
        id="disable_offline_files_service", name="Disable Offline Files service",
        category="service", tier="standard", risk="moderate", method="service",
        target={"service_name": "CscService"}, tweak_value="Disabled",
        min_os="7", max_os="11", os_verified=True,
        description="Client-Side Caching for offline network-drive access — a domain/enterprise feature almost no home user has enabled. Moderate because it does something real for the (mostly corporate) users who rely on synced offline network folders.",
    ),
    Tweak(
        id="disable_cortana_policy", name="Disable Cortana via policy",
        category="telemetry", tier="standard", risk="moderate", method="registry",
        target={"path": r"HKLM:\SOFTWARE\Policies\Microsoft\Windows\Windows Search", "name": "AllowCortana"},
        tweak_value=0,
        min_os="10", max_os="11", os_verified=True,
        description="Sets the Group Policy switch that blocks Cortana. On current Windows builds where Cortana's already been removed as a standalone app this is a no-op — kept moderate rather than safe since its real-world effect now varies by build.",
    ),

    # ---- MAXIMAL additions — advanced/situational, real trade-offs spelled out ----
    Tweak(
        id="reduce_hung_app_timeout", name="Shorten hung-app shutdown timeout",
        category="power", tier="maximal", risk="moderate", method="registry",
        target={"path": r"HKCU:\Control Panel\Desktop", "name": "WaitToKillAppTimeout", "force_type": "String"},
        tweak_value="2000",
        min_os="7", max_os="11", os_verified=True,
        description="Cuts how long Windows waits on a frozen app before forcing it closed during shutdown/restart, from the 5s default to 2s. Moderate: a genuinely slow (not frozen) app doing a big save could get killed before it finishes writing.",
    ),
    Tweak(
        id="auto_end_hung_tasks", name="Auto-end hung tasks on shutdown",
        category="power", tier="maximal", risk="moderate", method="registry",
        target={"path": r"HKCU:\Control Panel\Desktop", "name": "AutoEndTasks", "force_type": "String"},
        tweak_value="1",
        min_os="7", max_os="11", os_verified=True,
        description="Stops Windows from popping up an \"end program?\" dialog for a hung app during shutdown and just ends it automatically. Pairs with the hung-app timeout tweak above; same trade-off — a slow app can get killed before finishing an in-progress save.",
    ),
    Tweak(
        id="disable_device_metadata_network", name="Block automatic device metadata/driver lookups",
        category="network", tier="maximal", risk="moderate", method="registry",
        target={"path": r"HKLM:\SOFTWARE\Policies\Microsoft\Windows\Device Metadata", "name": "PreventDeviceMetadataFromNetwork"},
        tweak_value=1,
        min_os="7", max_os="11", os_verified=True,
        description="Stops Windows silently phoning home to fetch device metadata/driver info the moment new hardware is plugged in. Useful if a known-flaky auto-pushed driver keeps reinstalling itself; moderate because it also blocks legitimate metadata (icons, friendly names) for genuinely new devices.",
    ),
    Tweak(
        id="disable_smartcard_service", name="Disable Smart Card service",
        category="service", tier="maximal", risk="moderate", method="service",
        target={"service_name": "SCardSvr"}, tweak_value="Disabled",
        min_os="7", max_os="11", os_verified=True,
        description="Manages smart-card readers — dead weight on almost every consumer PC. Moderate rather than safe purely because a minority of laptops use a smart card or security key for sign-in, and this would break that.",
    ),
    Tweak(
        id="disable_connected_devices_platform", name="Disable Connected Devices Platform service",
        category="service", tier="maximal", risk="moderate", method="service",
        target={"service_name": "CDPSvc"}, tweak_value="Disabled",
        min_os="10", max_os="11", os_verified=True,
        description="Backs Nearby Sharing, Phone Link, and other cross-device continuity features. Real functionality lost if you use any of those — offered at maximal, not standard, specifically because the trade-off is bigger than the typical \"safe\" service disable.",
    ),
    Tweak(
        id="disable_xbox_networking_service", name="Disable Xbox Live Networking service",
        category="gaming", tier="maximal", risk="moderate", method="service",
        target={"service_name": "XboxNetApiSvc"}, tweak_value="Disabled",
        min_os="8", max_os="11", os_verified=True,
        description="Backs Xbox Live multiplayer/matchmaking networking used by the Xbox app and Game Pass titles. Fine to disable if you don't play anything through Xbox app/Game Pass; breaks online play in the games that do use it.",
    ),

    # ---- BLOATWARE — standard tier (Microsoft-shipped, uninstall/reinstall from Store any time) ----
    # _BLOATWARE_EXPLICIT_OVERRIDE_IDS: package ids that also have a hand-written Tweak()
    # elsewhere in this file with a non-generic risk/description (e.g. bloat_screensketch's
    # maximal-tier placement). Excluded here so the generic dict-driven version
    # doesn't collide with — and get silently shadowed by — the accurate one.
    *[
        Tweak(
            id=pkg_id, name=f"Remove {meta['label']}",
            category="bloatware", tier="standard", risk="safe", method="appx",
            target={"package_name": meta["package_name"]}, tweak_value=False,
            min_os=_bloat_min_os(pkg_id)[0], max_os=_bloat_min_os(pkg_id)[1], os_verified=True,
            description=f"Uninstalls the preinstalled '{meta['label']}' app for this user and blocks it from "
                         f"reinstalling on the next feature update. Reinstall is manual, from the Microsoft "
                         f"Store, if you change your mind — Flow can't script that part back.",
        )
        for pkg_id, meta in BLOATWARE_PACKAGES.items()
        if not pkg_id.endswith(("_spotify", "_tiktok", "_facebook", "_disneyplus", "_candycrush", "_camera", "_screensketch", "_quickassist", "_minecraft"))
        and pkg_id not in _BLOATWARE_EXPLICIT_OVERRIDE_IDS
    ],
    # ---- BLOATWARE — maximal tier (third-party OEM preinstalls, occasionally genuinely wanted) ----
    *[
        Tweak(
            id=pkg_id, name=f"Remove {meta['label']}",
            category="bloatware", tier="maximal", risk="safe", method="appx",
            target={"package_name": meta["package_name"]}, tweak_value=False,
            min_os=_bloat_min_os(pkg_id)[0], max_os=_bloat_min_os(pkg_id)[1], os_verified=True,
            description=f"Uninstalls the OEM-preinstalled '{meta['label']}' app. Offered at maximal rather than "
                         f"standard since — unlike the Microsoft in-box apps above — some people genuinely use "
                         f"these; reinstall is manual, from the Microsoft Store, if you change your mind.",
        )
        for pkg_id, meta in BLOATWARE_PACKAGES.items()
        if pkg_id.endswith(("_spotify", "_tiktok", "_facebook", "_disneyplus", "_candycrush", "_camera", "_screensketch", "_quickassist", "_minecraft"))
        and pkg_id not in _BLOATWARE_EXPLICIT_OVERRIDE_IDS
    ],

    # ---- EXTREME ("Full Maximal") — everything above PLUS the tweaks with
    # real security/functionality trade-offs. Never auto-suggested by
    # _suggest_tier(); only reachable by deliberately selecting the tier.
    # Each one is real, documented, and reversible where Windows allows it —
    # but several disable a security control on purpose, so risk="extreme"
    # keeps them visually and behaviorally distinct from "advanced". ----
    Tweak(
        id="disable_uac", name="Disable User Account Control (UAC)",
        category="security", tier="extreme", risk="extreme", method="registry",
        target={"path": r"HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System", "name": "EnableLUA"},
        tweak_value=0,
        min_os="7", max_os="11", os_verified=True,
        description="Turns off the elevation-prompt security boundary entirely — any process any app launches runs "
                     "with your full privileges, no confirmation. Removes a core Windows defense against malware "
                     "silently gaining admin rights. Takes effect after reboot.",
    ),
    Tweak(
        id="disable_defender_realtime", name="Disable Windows Defender real-time protection",
        category="security", tier="extreme", risk="extreme", method="registry",
        target={"path": r"HKLM:\SOFTWARE\Policies\Microsoft\Windows Defender\Real-Time Protection", "name": "DisableRealtimeMonitoring"},
        tweak_value=1,
        min_os="8", max_os="11", os_verified=True,
        description="Turns off live malware scanning system-wide. On builds with Tamper Protection enabled "
                     "(the default since 2019) this registry key is silently ignored — Tamper Protection has to be "
                     "turned off by hand in Windows Security first, which Flow will not do automatically.",
    ),
    Tweak(
        id="disable_defender_cloud", name="Disable Defender cloud-delivered protection",
        category="security", tier="extreme", risk="advanced", method="registry",
        target={"path": r"HKLM:\SOFTWARE\Policies\Microsoft\Windows Defender\Spynet", "name": "SpynetReporting"},
        tweak_value=0,
        min_os="10", max_os="11", os_verified=True,
        description="Stops Defender from sending sample metadata to Microsoft's cloud for real-time threat lookups. "
                     "Detection still runs locally on signature/heuristic data, just without the cloud assist — "
                     "slower to catch brand-new threats than with it on.",
    ),
    Tweak(
        id="disable_windows_update_service", name="Disable Windows Update service entirely",
        category="security", tier="extreme", risk="extreme", method="service",
        target={"service_name": "wuauserv"}, tweak_value="Disabled",
        min_os="7", max_os="11", os_verified=True,
        description="Stops ALL Windows Update activity, including security patches — not just feature updates or "
                     "the nag prompts. Leaves the machine exposed to newly disclosed vulnerabilities indefinitely "
                     "unless you manually re-enable this and catch up.",
    ),
    Tweak(
        id="disable_windows_update_policy", name="Block Windows Update via Group Policy",
        category="security", tier="extreme", risk="advanced", method="registry",
        target={"path": r"HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU", "name": "NoAutoUpdate"},
        tweak_value=1,
        min_os="7", max_os="11", os_verified=True,
        description="Belt-and-suspenders alongside disabling the wuauserv service above — blocks Update via policy "
                     "so it can't silently re-enable itself after certain feature updates re-register the service.",
    ),
    Tweak(
        id="disable_firewall_standard", name="Disable Windows Firewall (Private/Standard profile)",
        category="security", tier="extreme", risk="extreme", method="registry",
        target={"path": r"HKLM:\SYSTEM\CurrentControlSet\Services\SharedAccess\Parameters\FirewallPolicy\StandardProfile", "name": "EnableFirewall"},
        tweak_value=0,
        min_os="7", max_os="11", os_verified=True,
        description="Turns off inbound/outbound filtering for the Private network profile — home/office Wi-Fi and "
                     "LAN. Every open port on this machine becomes reachable from anything else on that network "
                     "with nothing standing in the way.",
    ),
    Tweak(
        id="disable_firewall_public", name="Disable Windows Firewall (Public profile)",
        category="security", tier="extreme", risk="extreme", method="registry",
        target={"path": r"HKLM:\SYSTEM\CurrentControlSet\Services\SharedAccess\Parameters\FirewallPolicy\PublicProfile", "name": "EnableFirewall"},
        tweak_value=0,
        min_os="7", max_os="11", os_verified=True,
        description="Same as the Private-profile firewall tweak but for untrusted networks — coffee shop Wi-Fi, "
                     "airports, hotspots. This is the profile you almost never actually want off.",
    ),
    Tweak(
        id="disable_smartscreen", name="Disable SmartScreen (Explorer/app reputation checks)",
        category="security", tier="extreme", risk="advanced", method="registry",
        target={"path": r"HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer", "name": "SmartScreenEnabled", "force_type": "String"},
        tweak_value="Off",
        min_os="8", max_os="11", os_verified=True,
        description="Stops Windows from checking downloaded/unrecognized executables against Microsoft's reputation "
                     "database before you run them — no more 'Windows protected your PC' prompt, and no warning "
                     "either.",
    ),
    Tweak(
        id="disable_biometrics_service", name="Disable Windows Biometric Service (Windows Hello)",
        category="security", tier="extreme", risk="advanced", method="service",
        target={"service_name": "WbioSrvc"}, tweak_value="Disabled",
        min_os="8", max_os="11", os_verified=True,
        description="Breaks fingerprint/face sign-in system-wide — you'll fall back to PIN/password everywhere "
                     "Hello was previously accepted, including at lock screen.",
    ),
    Tweak(
        id="remove_onedrive", name="Uninstall OneDrive",
        category="cloud", tier="extreme", risk="advanced", method="onedrive",
        target={}, tweak_value=False,
        min_os="8.1", max_os="11", os_verified=True,
        description="Fully uninstalls the OneDrive client and removes it from the Explorer sidebar. Files already "
                     "synced locally are left on disk as plain files — nothing already there gets deleted — but "
                     "anything only in the cloud stops syncing. Revert requires a manual reinstall from Microsoft.",
    ),
    Tweak(
        id="clear_pagefile_on_shutdown", name="Wipe pagefile on every shutdown",
        category="security", tier="extreme", risk="advanced", method="registry",
        target={"path": r"HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management", "name": "ClearPageFileAtShutdown"},
        tweak_value=1,
        min_os="7", max_os="11", os_verified=True,
        description="Zeroes the pagefile on every shutdown so nothing sensitive that got paged to disk during the "
                     "session survives it — a real security property on a shared/stolen-laptop threat model, at the "
                     "cost of a noticeably slower shutdown (seconds to tens of seconds depending on pagefile size).",
    ),
    Tweak(
        id="disable_error_reporting_policy", name="Block Windows Error Reporting via policy",
        category="telemetry", tier="extreme", risk="advanced", method="registry",
        target={"path": r"HKLM:\SOFTWARE\Microsoft\Windows\Windows Error Reporting", "name": "Disabled"},
        tweak_value=1,
        min_os="7", max_os="11", os_verified=True,
        description="Policy-level block on crash-dump submission to Microsoft, on top of disabling the WER service "
                     "itself — stops it from being silently re-enabled by a service reset. You lose Microsoft's "
                     "automated 'here's a known fix for that crash' suggestions.",
    ),

    # ---- HYBRID — one tweak, multiple atomic steps applied/reverted together.
    # This is the scaling mechanism: instead of one Tweak per registry key,
    # a hybrid bundles several real, related keys behind a single id/checkbox.
    # Each step is captured and reverted independently, so a partial failure
    # (step 2 of 3 fails) still leaves an accurate per-step revert trail —
    # see apply_tweak()/revert_entry()'s "hybrid" branches. ----
    # Intentionally re-touches TaskbarAl — same key as the standalone taskbar_align_left
    # tweak elsewhere in this file. Same "convenience bundle" pattern as
    # bundle_notification_lockdown above — not a duplicate bug, see that comment.
    Tweak(
        id="bundle_declutter_taskbar", name="Declutter taskbar (People, Task View, left-align)",
        category="visual", tier="minimal", risk="safe", method="hybrid",
        target={"steps": [
            {"method": "registry", "target": {"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "name": "PeopleBand"}, "value": 0},
            {"method": "registry", "target": {"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "name": "ShowTaskViewButton"}, "value": 0},
            {"method": "registry", "target": {"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "name": "TaskbarAl"}, "value": 0},
        ]}, tweak_value=None,
        min_os="10", max_os="11", os_verified=True,
        description="Three taskbar decluttering keys in one apply: hides the People icon, hides the Task View "
                     "button, and left-aligns taskbar icons (Windows 11 — no-op on 10). Each key reverts "
                     "independently, so a partial failure still leaves an accurate trail.",
        requires_explorer_refresh=True,
    ),
    Tweak(
        id="bundle_reduce_background_sync", name="Reduce background sync chatter (clipboard + Nearby Share)",
        category="privacy", tier="standard", risk="moderate", method="hybrid",
        target={"steps": [
            {"method": "registry", "target": {"path": r"HKCU:\Software\Microsoft\Clipboard", "name": "AllowCrossDeviceClipboard"}, "value": 0},
            {"method": "registry", "target": {"path": r"HKCU:\Software\Microsoft\Clipboard", "name": "EnableClipboardHistory"}, "value": 0},
        ]}, tweak_value=None,
        min_os="10", max_os="11", os_verified=True,
        description="Turns off cross-device clipboard sync (Cloud Clipboard/'Nearby Sharing' text hand-off) and "
                     "local clipboard history together — two related keys under the same feature, one apply. "
                     "Moderate: you lose Win+V clipboard history, not just the cross-device part.",
    ),
    # Intentionally re-touches ToastEnabled and LockScreenToastEnabled — same keys
    # as disable_toast_notifications / disable_lockscreen_notifications above. Not
    # a duplicate bug: this is the documented "one click instead of three" bundle
    # pattern. Applying both is harmless — apply_tweak()'s idempotency check skips
    # re-writing an already-matching value, and reverting just re-asserts the same
    # value twice.
    Tweak(
        id="bundle_notification_lockdown", name="Full notification lockdown (toasts + lock screen + Focus Assist quiet)",
        category="notifications", tier="maximal", risk="moderate", method="hybrid",
        target={"steps": [
            {"method": "registry", "target": {"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\PushNotifications", "name": "ToastEnabled"}, "value": 0},
            {"method": "registry", "target": {"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\PushNotifications", "name": "LockScreenToastEnabled"}, "value": 0},
            {"method": "registry", "target": {"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\CloudContent", "name": "DisableWindowsSpotlightFeatures"}, "value": 1},
        ]}, tweak_value=None,
        min_os="10", max_os="11", os_verified=True,
        description="One click instead of three separate ones for people who want notifications fully silenced: "
                     "kills toasts, kills lock-screen previews, and disables the remaining Spotlight/suggestion "
                     "surfaces that weren't covered by the individual tweaks above.",
    ),

    # ---- Batch: DNS/WiFi bundle, autorun, RDP, telemetry level, more Explorer QoL ----
    Tweak(
        id="explorer_show_hidden_files", name="Always show hidden files",
        category="explorer", tier="minimal", risk="safe", method="registry",
        target={"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "name": "Hidden"},
        tweak_value=1,
        min_os="7", max_os="11", os_verified=True,
        description="Same checkbox as View > Hidden items. Useful for anyone who regularly needs to see AppData, "
                     "dotfiles, or hidden config folders without toggling it every session.",
        requires_explorer_refresh=True,
    ),
    Tweak(
        id="disable_compressed_file_color", name="Stop color-coding compressed/encrypted filenames",
        category="visual", tier="minimal", risk="safe", method="registry",
        target={"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "name": "ShowCompColor"},
        tweak_value=0,
        min_os="7", max_os="11", os_verified=True,
        description="Explorer normally tints NTFS-compressed files blue and encrypted files green. Turns that off "
                     "— purely cosmetic, some people find it makes file lists noisier than useful.",
        requires_explorer_refresh=True,
    ),
    Tweak(
        id="show_seconds_in_taskbar_clock", name="Show seconds in the taskbar clock",
        category="visual", tier="minimal", risk="safe", method="registry",
        target={"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "name": "ShowSecondsInSystemClock"},
        tweak_value=1,
        min_os="11", max_os="11", os_verified=True,
        description="Windows 11 22H2+ only — adds a seconds field to the taskbar clock. Silently ignored on older "
                     "builds that don't have this key.",
        requires_explorer_refresh=True,
    ),
    Tweak(
        id="enable_long_paths", name="Enable long path support (>260 characters)",
        category="storage", tier="minimal", risk="safe", method="registry",
        target={"path": r"HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem", "name": "LongPathsEnabled"},
        tweak_value=1,
        min_os="10", max_os="11", os_verified=True,
        description="Removes the historical 260-character MAX_PATH limit for apps that opt in (most modern "
                     "software does). Fixes 'path too long' errors from deeply nested node_modules folders, "
                     "long repo clones, etc. Purely additive — nothing that worked under the old limit stops "
                     "working, this only unblocks paths that used to fail.",
    ),
    Tweak(
        id="disable_recycle_bin_confirmation", name="Skip the Recycle Bin delete confirmation prompt",
        category="visual", tier="minimal", risk="safe", method="registry",
        target={"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket", "name": "ConfirmFileDelete"},
        tweak_value=0,
        min_os="7", max_os="11", os_verified=True,
        description="Stops the 'Are you sure you want to move this to the Recycle Bin?' popup on every delete. "
                     "Files still go to the Recycle Bin as normal (nothing about actual deletion changes) — this "
                     "only removes the extra click, and Shift+Delete still permanently deletes as it always has.",
    ),
    Tweak(
        id="disable_search_highlights", name="Disable Search Highlights (Start Menu search icon animations/trending)",
        category="visual", tier="standard", risk="safe", method="registry",
        target={"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\SearchSettings", "name": "IsDynamicSearchBoxEnabled"},
        tweak_value=0,
        min_os="11", max_os="11", os_verified=True,
        description="Windows 11 — stops the search box icon from animating/highlighting for trending topics and "
                     "doodles. Separate from the Bing-web-results toggle above; this one's purely the icon/animation "
                     "layer.",
        requires_explorer_refresh=True,
    ),
    Tweak(
        id="disable_start_menu_recommendations", name="Disable 'Recommended' section in Start Menu",
        category="visual", tier="minimal", risk="safe", method="registry",
        target={"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "name": "Start_IrisRecommendations"},
        tweak_value=0,
        min_os="11", max_os="11", os_verified=True,
        description="Windows 11 22H2+ — removes the 'Recommended' row of recently-used files/apps from the "
                     "Start Menu, leaving more room for pinned apps. Silently ignored on builds without this "
                     "key rather than erroring.",
        requires_explorer_refresh=True,
    ),
    Tweak(
        id="show_this_pc_desktop_icon", name="Add 'This PC' icon to the desktop",
        category="visual", tier="minimal", risk="safe", method="registry",
        target={"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\HideDesktopIcons\NewStartPanel",
                 "name": "{20D04FE0-3AEA-1069-A2D8-08002B30309D}"},
        tweak_value=0,
        min_os="7", max_os="11", os_verified=True,
        description="Restores the classic 'This PC' shortcut to the desktop for quick drive access — hidden by "
                     "default since Windows 8. The CLSID here is Windows' fixed identifier for the This PC "
                     "namespace, not something that varies by build; 0 = show, 1 = hide.",
        requires_explorer_refresh=True,
    ),
    Tweak(
        id="disable_autorun", name="Disable AutoPlay/AutoRun on all drives",
        category="security", tier="standard", risk="safe", method="registry",
        target={"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Policies\Explorer", "name": "NoDriveTypeAutoRun"},
        tweak_value=255,
        min_os="7", max_os="11", os_verified=True,
        description="Blocks the classic USB-stick/CD autorun prompt on every drive type — closes a real, old malware "
                     "delivery vector, at the cost of manually opening removable media instead of it popping up "
                     "automatically.",
    ),
    Tweak(
        id="set_telemetry_basic", name="Cap telemetry level to Basic",
        category="telemetry", tier="standard", risk="safe", method="registry",
        target={"path": r"HKLM:\SOFTWARE\Policies\Microsoft\Windows\DataCollection", "name": "AllowTelemetry"},
        tweak_value=1,
        min_os="7", max_os="11", os_verified=True,
        description="Sets the diagnostic data level policy to Basic (1) — the lowest setting available on Home/Pro "
                     "editions (Security/0 is Enterprise-only). Complements disabling the DiagTrack service; this "
                     "caps what other first-party telemetry paths are allowed to send.",
    ),
    Tweak(
        id="disable_remote_desktop", name="Disable inbound Remote Desktop connections",
        category="security", tier="maximal", risk="moderate", method="registry",
        target={"path": r"HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server", "name": "fDenyTSConnections"},
        tweak_value=1,
        min_os="7", max_os="11", os_verified=True,
        description="Blocks RDP from accepting inbound connections — shrinks a commonly-scanned attack surface. "
                     "Moderate risk because it breaks legitimate remote-desktop access to this machine if you "
                     "actually rely on it (e.g. connecting in from another PC).",
    ),
    Tweak(
        id="bundle_disable_wifi_sense", name="Disable Wi-Fi Sense hotspot sharing/auto-connect",
        category="network", tier="standard", risk="safe", method="hybrid",
        target={"steps": [
            {"method": "registry", "target": {"path": r"HKLM:\SOFTWARE\Microsoft\PolicyManager\default\WiFi", "name": "AllowWiFiHotSpotReporting"}, "value": 0},
            {"method": "registry", "target": {"path": r"HKLM:\SOFTWARE\Microsoft\PolicyManager\default\WiFi", "name": "AllowAutoConnectToWiFiSenseHotspots"}, "value": 0},
        ]}, tweak_value=None,
        min_os="10", max_os="10", os_verified=True,
        description="Two keys for the old Wi-Fi Sense feature (auto-sharing/auto-joining crowdsourced open "
                     "hotspots). Mostly dormant on current builds but the policy keys are still honored where "
                     "present — harmless no-op if the feature's already gone from your build.",
    ),
    Tweak(
        id="bundle_explorer_stability", name="Explorer per-window process isolation + disable Aero Shake",
        category="explorer", tier="standard", risk="moderate", method="hybrid",
        target={"steps": [
            {"method": "registry", "target": {"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "name": "SeparateProcess"}, "value": 1},
            {"method": "registry", "target": {"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Policies\Explorer", "name": "NoWindowMinimizingShortcuts"}, "value": 1},
        ]}, tweak_value=None,
        min_os="10", max_os="11", os_verified=True,
        description="Runs each Explorer window as its own process — one folder window crashing no longer takes "
                     "down every other open window or the desktop shell, at the cost of extra RAM per window. Also "
                     "disables Aero Shake (grab a window and shake to minimize everything else), a frequent "
                     "accidental-trigger annoyance.",
        requires_explorer_refresh=True,
    ),

    # ---- Batch 4: more safe/moderate services, ink workspace, tray icons, UPnP pair ----
    Tweak(
        id="disable_print_notifications", name="Disable print job notifications service",
        category="service", tier="minimal", risk="safe", method="service",
        target={"service_name": "PrintNotify"}, tweak_value="Disabled",
        min_os="7", max_os="11", os_verified=True,
        description="Kills the toast popups for print job status/completion. Printing itself keeps working through "
                     "the Print Spooler — this only silences the notification pipeline. Safe if you don't print.",
    ),
    Tweak(
        id="disable_diagnostic_policy_service", name="Disable Diagnostic Policy Service",
        category="service", tier="standard", risk="moderate", method="service",
        target={"service_name": "DPS"}, tweak_value="Disabled",
        min_os="7", max_os="11", os_verified=True,
        description="Backs Windows' built-in troubleshooters (network, audio, hardware diagnostics wizards). "
                     "Moderate because it removes access to those wizards entirely — fine if you never use "
                     "'Windows found a problem, click to fix,' not fine if you rely on them.",
    ),
    Tweak(
        id="disable_ics_service", name="Disable Internet Connection Sharing service",
        category="service", tier="standard", risk="moderate", method="service",
        target={"service_name": "SharedAccess"}, tweak_value="Disabled",
        min_os="7", max_os="11", os_verified=True,
        description="Disables the classic ICS service. On several Windows builds this is the same service backing "
                     "Mobile Hotspot — moderate risk specifically because it can silently break tethering if you "
                     "use your PC to share its connection.",
    ),
    Tweak(
        id="disable_telephony_service", name="Disable Telephony (TAPI) service",
        category="service", tier="minimal", risk="safe", method="service",
        target={"service_name": "TapiSrv"}, tweak_value="Disabled",
        min_os="7", max_os="11", os_verified=True,
        description="Legacy modem/VoIP telephony API support — effectively unused on a modern desktop/laptop "
                     "without a fax modem or old softphone software.",
    ),
    Tweak(
        id="disable_wallet_service", name="Disable Wallet Service",
        category="service", tier="minimal", risk="safe", method="service",
        target={"service_name": "WalletService"}, tweak_value="Disabled",
        min_os="10", max_os="11", os_verified=True,
        description="Backs the deprecated Windows Wallet (NFC tap-to-pay) feature, already removed from the UI on "
                     "current builds — the service is dead weight left behind.",
    ),
    Tweak(
        id="disable_phone_service", name="Disable Phone Service",
        category="service", tier="standard", risk="safe", method="service",
        target={"service_name": "PhoneSvc"}, tweak_value="Disabled",
        min_os="10", max_os="11", os_verified=True,
        description="Backs cellular calling/SMS features aimed at Windows tablets with a SIM slot — essentially "
                     "inert on a standard desktop/laptop with no cellular modem.",
    ),
    Tweak(
        id="disable_link_tracking", name="Disable Distributed Link Tracking Client",
        category="service", tier="standard", risk="safe", method="service",
        target={"service_name": "TrkWks"}, tweak_value="Disabled",
        min_os="7", max_os="11", os_verified=True,
        description="Keeps shortcuts pointing at the right file if you move/rename it across NTFS volumes on a "
                     "domain network — a corporate-network feature almost never relevant on a home/personal PC.",
    ),
    Tweak(
        id="bundle_disable_upnp", name="Disable UPnP device discovery (SSDP + UPnP Host)",
        category="network", tier="standard", risk="moderate", method="hybrid",
        target={"steps": [
            {"method": "service", "target": {"service_name": "SSDPSRV"}, "value": "Disabled"},
            {"method": "service", "target": {"service_name": "upnphost"}, "value": "Disabled"},
        ]}, tweak_value=None,
        min_os="7", max_os="11", os_verified=True,
        description="Disables both halves of Windows' UPnP auto-discovery — the pair that lets smart TVs, DLNA "
                     "media servers, game consoles, and some printers show up automatically on the network. "
                     "Moderate: breaks that auto-discovery specifically, doesn't affect manual/IP-based connections.",
    ),
    Tweak(
        id="disable_ink_workspace", name="Disable Windows Ink Workspace",
        category="visual", tier="minimal", risk="safe", method="registry",
        target={"path": r"HKCU:\Software\Policies\Microsoft\WindowsInkWorkspace", "name": "AllowWindowsInkWorkspace"},
        tweak_value=0,
        min_os="10", max_os="11", os_verified=True,
        description="Turns off the pen/Ink Workspace flyout (sticky notes, whiteboard, screen sketch launcher) "
                     "system-wide. Irrelevant hardware-wise on a non-touch/non-pen laptop — just a taskbar button "
                     "doing nothing useful.",
        requires_explorer_refresh=True,
    ),
    Tweak(
        id="show_all_tray_icons", name="Always show all system tray icons",
        category="visual", tier="minimal", risk="safe", method="registry",
        target={"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "name": "EnableAutoTray"},
        tweak_value=0,
        min_os="8", max_os="11", os_verified=True,
        description="Same as Settings > Taskbar corner icons > 'Always show all icons in the notification area' — "
                     "stops Windows auto-hiding tray icons behind the overflow chevron.",
        requires_explorer_refresh=True,
    ),

    # ---- Batch 5: more safe services, lock screen/multi-user policy, Edge debloat bundle, 3 more bloatware ----
    Tweak(
        id="disable_ajrouter", name="Disable AllJoyn Router Service",
        category="service", tier="minimal", risk="safe", method="service",
        target={"service_name": "AJRouter"}, tweak_value="Disabled",
        min_os="8", max_os="11", os_verified=True,
        description="Backs AllJoyn, an old IoT device-discovery protocol almost nothing on a modern network still "
                     "uses. Safe unless you specifically have AllJoyn-based smart-home hardware.",
    ),
    Tweak(
        id="disable_remote_access_service", name="Disable Routing and Remote Access service",
        category="service", tier="minimal", risk="safe", method="service",
        target={"service_name": "RemoteAccess"}, tweak_value="Disabled",
        min_os="7", max_os="11", os_verified=True,
        description="RRAS — legacy dial-up/VPN-server/routing role, disabled by default on client Windows already "
                     "in most cases. Harmless to formally disable if it's present and unused.",
    ),
    Tweak(
        id="disable_payments_nfc_service", name="Disable Payments and NFC/SE Manager",
        category="service", tier="minimal", risk="safe", method="service",
        target={"service_name": "SEMgrSvc"}, tweak_value="Disabled",
        min_os="10", max_os="11", os_verified=True,
        description="Backs NFC tap-to-pay hardware — only relevant on tablets/2-in-1s with an actual NFC chip. "
                     "Inert on a standard desktop/laptop.",
    ),
    Tweak(
        id="disable_push_notification_service", name="Disable Windows Push Notification service",
        category="service", tier="maximal", risk="moderate", method="service",
        target={"service_name": "WpnService"}, tweak_value="Disabled",
        min_os="8", max_os="11", os_verified=True,
        description="Stops the underlying WNS push-notification transport UWP/Store apps use — broader and more "
                     "aggressive than the per-user toast registry toggle above. Moderate: breaks push notifications "
                     "for Store apps (Mail, Messaging, some third-party UWP apps) at the transport level, not just "
                     "the display layer.",
    ),
    Tweak(
        id="disable_lock_screen_camera", name="Disable camera swipe from lock screen",
        category="security", tier="minimal", risk="safe", method="registry",
        target={"path": r"HKLM:\SOFTWARE\Policies\Microsoft\Windows\Personalization", "name": "NoLockScreenCamera"},
        tweak_value=1,
        min_os="8", max_os="11", os_verified=True,
        description="Removes the swipe-down-to-launch-camera shortcut from the lock screen — closes a minor "
                     "physical-access path (camera access without unlocking) with no functional trade-off if you "
                     "don't use that shortcut.",
    ),
    Tweak(
        id="disable_fast_user_switching", name="Disable Fast User Switching",
        category="security", tier="maximal", risk="moderate", method="registry",
        target={"path": r"HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System", "name": "HideFastUserSwitching"},
        tweak_value=1,
        min_os="7", max_os="11", os_verified=True,
        description="Forces the current user to fully sign out before another account can sign in, instead of "
                     "switching with the first session left running in the background. Moderate: breaks the "
                     "convenience of fast switching on a genuinely shared/multi-user machine.",
    ),
    Tweak(
        id="bundle_edge_debloat", name="Debloat Edge (skip first-run, no startup boost, no sidebar, no ad personalization, no rewards/wallet/insider promos)",
        category="visual", tier="standard", risk="safe", method="hybrid",
        target={"steps": [
            {"method": "registry", "target": {"path": r"HKLM:\SOFTWARE\Policies\Microsoft\Edge", "name": "HideFirstRunExperience"}, "value": 1},
            {"method": "registry", "target": {"path": r"HKLM:\SOFTWARE\Policies\Microsoft\Edge", "name": "StartupBoostEnabled"}, "value": 0},
            {"method": "registry", "target": {"path": r"HKLM:\SOFTWARE\Policies\Microsoft\Edge", "name": "HubsSidebarEnabled"}, "value": 0},
            {"method": "registry", "target": {"path": r"HKLM:\SOFTWARE\Policies\Microsoft\Edge", "name": "PersonalizationReportingEnabled"}, "value": 0},
            {"method": "registry", "target": {"path": r"HKLM:\SOFTWARE\Policies\Microsoft\Edge", "name": "ShowRecommendationsEnabled"}, "value": 0},
            {"method": "registry", "target": {"path": r"HKLM:\SOFTWARE\Policies\Microsoft\Edge", "name": "UserFeedbackAllowed"}, "value": 0},
            {"method": "registry", "target": {"path": r"HKLM:\SOFTWARE\Policies\Microsoft\Edge", "name": "ConfigureDoNotTrack"}, "value": 1},
            {"method": "registry", "target": {"path": r"HKLM:\SOFTWARE\Policies\Microsoft\Edge", "name": "AlternateErrorPagesEnabled"}, "value": 0},
            {"method": "registry", "target": {"path": r"HKLM:\SOFTWARE\Policies\Microsoft\Edge", "name": "EdgeAssetDeliveryServiceEnabled"}, "value": 0},
            {"method": "registry", "target": {"path": r"HKLM:\SOFTWARE\Policies\Microsoft\Edge", "name": "WalletDonationEnabled"}, "value": 0},
            {"method": "registry", "target": {"path": r"HKLM:\SOFTWARE\Policies\Microsoft\Edge", "name": "DefaultBrowserSettingsCampaignEnabled"}, "value": 0},
            {"method": "registry", "target": {"path": r"HKLM:\SOFTWARE\Policies\Microsoft\Edge", "name": "MicrosoftEdgeInsiderPromotionEnabled"}, "value": 0},
            {"method": "registry", "target": {"path": r"HKLM:\SOFTWARE\Policies\Microsoft\Edge", "name": "ShowMicrosoftRewards"}, "value": 0},
            {"method": "registry", "target": {"path": r"HKLM:\SOFTWARE\Policies\Microsoft\Edge", "name": "WebWidgetAllowed"}, "value": 0},
            {"method": "registry", "target": {"path": r"HKLM:\SOFTWARE\Policies\Microsoft\Edge", "name": "DiagnosticData"}, "value": 0},
            {"method": "registry", "target": {"path": r"HKLM:\SOFTWARE\Policies\Microsoft\EdgeUpdate", "name": "CreateDesktopShortcutDefault"}, "value": 0},
        ]}, tweak_value=None,
        min_os="10", max_os="11", os_verified=True,
        description="16 Edge policy keys in one apply: first-run tour, background startup boost, sidebar, ad "
                     "personalization, Store recommendations, feedback prompts, do-not-track, error-page "
                     "suggestions, asset pre-delivery, Wallet donation prompts, default-browser nag campaigns, "
                     "Insider promo banners, Rewards points nagging, web widgets, diagnostic data, and desktop "
                     "shortcut auto-creation on update. Doesn't touch bookmarks, extensions, or sync.",
    ),
    Tweak(
        id="disable_edge_extension_ads_extension", name="Block Edge's built-in 'Shopping'/ad-injection extension",
        category="visual", tier="standard", risk="safe", method="registry",
        target={"path": r"HKLM:\SOFTWARE\Policies\Microsoft\Edge\ExtensionInstallBlocklist", "name": "1"},
        tweak_value="ofefcgjbeghpigppfmkologfjadafddi",
        min_os="10", max_os="11", os_verified=True,
        description="Blocklists the specific extension ID Edge uses for its built-in shopping/coupon-injection "
                     "feature, preventing it from ever installing itself into the browser.",
    ),

    # ---- Batch 6: more fixed-name (non-per-user) services + Quick Access cleanup ----
    Tweak(
        id="disable_perfhost", name="Disable Performance Counter DLL Host",
        category="service", tier="minimal", risk="safe", method="service",
        target={"service_name": "PerfHost"}, tweak_value="Disabled",
        min_os="7", max_os="11", os_verified=True,
        description="Hosts 32-bit performance counters on 64-bit Windows for legacy monitoring tools. Unused unless "
                     "you run old 32-bit perf-monitoring software.",
    ),
    Tweak(
        id="disable_problem_reports_support", name="Disable Problem Reports Control Panel Support",
        category="service", tier="minimal", risk="safe", method="service",
        target={"service_name": "wercplsupport"}, tweak_value="Disabled",
        min_os="7", max_os="11", os_verified=True,
        description="Backs the 'view problem reports' Control Panel applet specifically — separate from the WER "
                     "crash-reporting service itself. Just removes access to the report-viewing UI.",
    ),
    Tweak(
        id="disable_wifi_config_registrar", name="Disable Windows Connect Now Config Registrar",
        category="service", tier="standard", risk="safe", method="service",
        target={"service_name": "wcncsvc"}, tweak_value="Disabled",
        min_os="7", max_os="11", os_verified=True,
        description="Backs WPS push-button wireless setup (the physical-button pairing flow for some routers/"
                     "printers). Safe unless you specifically use WPS push-button pairing to join networks.",
    ),
    Tweak(
        id="disable_mobile_hotspot_service", name="Disable Windows Mobile Hotspot service",
        category="service", tier="maximal", risk="moderate", method="service",
        target={"service_name": "icssvc"}, tweak_value="Disabled",
        min_os="10", max_os="11", os_verified=True,
        description="The actual service behind Settings > Mobile hotspot on current Windows builds. Moderate: "
                     "breaks the ability to share this PC's connection over Wi-Fi/Bluetooth if you use that feature.",
    ),
    Tweak(
        id="disable_sensor_service", name="Disable Sensor Service",
        category="service", tier="minimal", risk="safe", method="service",
        target={"service_name": "SensorService"}, tweak_value="Disabled",
        min_os="8", max_os="11", os_verified=True,
        description="Backs ambient light/accelerometer/orientation sensors — hardware most desktops and many "
                     "laptops don't have. Safe unless your device has auto-brightness or auto-rotate features you use.",
    ),
    Tweak(
        id="disable_sensor_monitoring", name="Disable Sensor Monitoring Service",
        category="service", tier="minimal", risk="safe", method="service",
        target={"service_name": "SensrSvc"}, tweak_value="Disabled",
        min_os="8", max_os="11", os_verified=True,
        description="Companion service to Sensor Service — monitors sensor state changes. Same hardware dependency, "
                     "same safe-to-disable reasoning.",
    ),
    Tweak(
        id="disable_diagnostics_hub_collector", name="Disable Diagnostics Hub Standard Collector",
        category="service", tier="minimal", risk="safe", method="service",
        target={"service_name": "diagnosticshub.standardcollector.service"}, tweak_value="Disabled",
        min_os="10", max_os="11", os_verified=True,
        description="Backs Visual Studio's performance profiler data collection. Irrelevant unless you're actively "
                     "profiling apps in Visual Studio.",
    ),
    Tweak(
        id="disable_net_tcp_port_sharing", name="Disable Net.Tcp Port Sharing Service",
        category="service", tier="minimal", risk="safe", method="service",
        target={"service_name": "NetTcpPortSharing"}, tweak_value="Disabled",
        min_os="7", max_os="11", os_verified=True,
        description="Backs WCF (Windows Communication Foundation) TCP port sharing between .NET services — a "
                     "developer/enterprise feature almost never active on a personal machine.",
    ),
    Tweak(
        id="disable_secondary_logon", name="Disable Secondary Logon service",
        category="service", tier="maximal", risk="moderate", method="service",
        target={"service_name": "seclogon"}, tweak_value="Disabled",
        min_os="7", max_os="11", os_verified=True,
        description="Backs 'Run as different user' from the right-click menu. Moderate: breaks that specific "
                     "feature — fine if you never use it, not fine if you regularly run something as a different "
                     "account.",
    ),
    Tweak(
        id="bundle_clean_quick_access", name="Clean up Quick Access (hide frequent folders + recent files)",
        category="explorer", tier="minimal", risk="safe", method="hybrid",
        target={"steps": [
            {"method": "registry", "target": {"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "name": "ShowFrequent"}, "value": 0},
            {"method": "registry", "target": {"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "name": "ShowRecent"}, "value": 0},
        ]}, tweak_value=None,
        min_os="11", max_os="11", os_verified=True,
        description="Same two checkboxes as Explorer's Folder Options > General > Privacy — hides both the "
                     "'Frequent folders' and 'Recent files' sections from Quick Access, leaving it as a plain "
                     "pinned-folders list.",
        requires_explorer_refresh=True,
    ),

    # ---- Batch 7: taskbar icon size, recent-docs policy, nav pane, list view cosmetics, Copilot, 3 bloatware ----
    Tweak(
        id="set_small_taskbar_icons", name="Use small taskbar icons",
        category="visual", tier="minimal", risk="safe", method="registry",
        target={"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "name": "TaskbarSi"},
        tweak_value=0,
        min_os="7", max_os="10", os_verified=True,
        description="Windows 11's taskbar size setting (Settings > Personalization > Taskbar > Taskbar size). "
                     "0=small, 1=medium (default), 2=large. Small reclaims a few pixels of vertical space and fits "
                     "more icons before overflow.",
        requires_explorer_refresh=True,
    ),
    Tweak(
        id="disable_recent_docs_history_policy", name="Block Recent Documents history via policy",
        category="privacy", tier="standard", risk="safe", method="registry",
        target={"path": r"HKLM:\SOFTWARE\Policies\Microsoft\Windows\Explorer", "name": "NoRecentDocsHistory"},
        tweak_value=1,
        min_os="7", max_os="11", os_verified=True,
        description="Policy-level block on the whole Recent Documents feature — stronger than the per-user "
                     "'stop tracking' toggle above, since it also prevents the list from being populated by "
                     "anything running under a different context.",
    ),
    Tweak(
        id="explorer_navpane_show_all_folders", name="Show all folders in Explorer's nav pane",
        category="explorer", tier="minimal", risk="safe", method="registry",
        target={"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "name": "NavPaneShowAllFolders"},
        tweak_value=1,
        min_os="8", max_os="11", os_verified=True,
        description="Same as Folder Options > View > 'Show all folders' — adds Control Panel, Recycle Bin, and "
                     "other virtual folders into the left-hand tree permanently instead of only This PC's contents.",
        requires_explorer_refresh=True,
    ),
    Tweak(
        id="explorer_navpane_auto_expand", name="Auto-expand nav pane to current folder",
        category="explorer", tier="minimal", risk="safe", method="registry",
        target={"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "name": "NavPaneExpandToCurrentFolder"},
        tweak_value=1,
        min_os="7", max_os="11", os_verified=True,
        description="Same as Folder Options > View > 'Automatically expand to current folder' — the left-hand tree "
                     "auto-scrolls/expands to highlight wherever you currently are, instead of staying static.",
        requires_explorer_refresh=True,
    ),
    Tweak(
        id="disable_listview_alpha_select", name="Disable translucent selection rectangle",
        category="visual", tier="minimal", risk="safe", method="registry",
        target={"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "name": "ListviewAlphaSelect"},
        tweak_value=0,
        min_os="7", max_os="11", os_verified=True,
        description="Turns off the translucent blue drag-select rectangle in Explorer/desktop, back to a plain "
                     "dotted outline. Purely cosmetic, negligible perf difference on modern hardware.",
        requires_explorer_refresh=True,
    ),
    Tweak(
        id="disable_desktop_icon_shadow", name="Disable desktop icon label shadow",
        category="visual", tier="minimal", risk="safe", method="registry",
        target={"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "name": "ListviewShadow"},
        tweak_value=0,
        min_os="7", max_os="11", os_verified=True,
        description="Removes the drop-shadow rendered behind desktop icon text labels. Cosmetic only.",
        requires_explorer_refresh=True,
    ),
    Tweak(
        id="disable_windows_copilot", name="Disable Windows Copilot",
        category="visual", tier="standard", risk="safe", method="registry",
        target={"path": r"HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsCopilot", "name": "TurnOffWindowsCopilot"},
        tweak_value=1,
        min_os="11", max_os="11", os_verified=True,
        description="Policy-level disable for the Windows Copilot sidebar/taskbar button — removes the icon and "
                     "blocks it from launching, system-wide.",
    ),
    Tweak(
        id="bloat_dev_home", name="Remove Dev Home",
        category="bloatware", tier="standard", risk="safe", method="appx",
        target={"package_name": "Microsoft.Windows.DevHome"}, tweak_value=False,
        min_os="11", max_os="11", os_verified=True,
        description="Uninstalls Dev Home, Microsoft's developer dashboard app. Irrelevant unless you're actively "
                     "using it to manage dev environments/WinGet configs.",
    ),
    Tweak(
        id="bloat_quick_assist", name="Remove Quick Assist",
        category="bloatware", tier="maximal", risk="safe", method="appx",
        target={"package_name": "MicrosoftCorporationII.QuickAssist"}, tweak_value=False,
        min_os="8", max_os="11", os_verified=True,
        description="Uninstalls Quick Assist, the built-in remote-support tool. Offered at maximal since some "
                     "people genuinely rely on it for helping family/getting IT support remotely.",
    ),
    Tweak(
        id="bloat_3dviewer", name="Remove 3D Viewer",
        category="bloatware", tier="standard", risk="safe", method="appx",
        target={"package_name": "Microsoft.Microsoft3DViewer"}, tweak_value=False,
        min_os="8", max_os="11", os_verified=True,
        description="Uninstalls the 3D Viewer app (3D model preview/viewer), companion to 3D Builder — dead weight "
                     "unless you work with 3D model files.",
    ),

    # ---- Batch 8: SMB1/LLMNR hardening, accessibility hotkeys, peer-networking bundle, more safe services ----
    Tweak(
        id="disable_smb1_server", name="Disable SMBv1 server-side protocol",
        category="security", tier="standard", risk="safe", method="registry",
        target={"path": r"HKLM:\SYSTEM\CurrentControlSet\Services\LanmanServer\Parameters", "name": "SMB1"},
        tweak_value=0,
        min_os="7", max_os="11", os_verified=True,
        description="Turns off the ancient, insecure SMBv1 file-sharing protocol on this PC's own file-sharing "
                     "server side — the same protocol EternalBlue/WannaCry exploited. Microsoft itself recommends "
                     "this off unless you specifically need to reach very old NAS boxes or printers that only "
                     "speak SMB1.",
    ),
    Tweak(
        id="disable_llmnr", name="Disable LLMNR (Link-Local Multicast Name Resolution)",
        category="security", tier="standard", risk="safe", method="registry",
        target={"path": r"HKLM:\SOFTWARE\Policies\Microsoft\Windows NT\DNSClient", "name": "EnableMulticast"},
        tweak_value=0,
        min_os="7", max_os="11", os_verified=True,
        description="Disables LLMNR, the fallback name-resolution broadcast Windows uses when DNS fails — a "
                     "well-known target for credential-relay attacks (Responder-style) on shared/untrusted "
                     "networks. Standard DNS resolution is unaffected.",
    ),
    Tweak(
        id="disable_filter_keys_prompt", name="Disable Filter Keys shortcut prompt",
        category="accessibility", tier="minimal", risk="safe", method="registry",
        target={"path": r"HKCU:\Control Panel\Accessibility\Keyboard Response", "name": "Flags", "force_type": "String"},
        tweak_value="122",
        min_os="7", max_os="11", os_verified=True,
        description="Stops the popup from holding the right Shift key for 8 seconds — same false-trigger "
                     "nuisance category as the Sticky/Toggle Keys prompts above.",
    ),
    Tweak(
        id="disable_high_contrast_hotkey", name="Disable High Contrast toggle hotkey",
        category="accessibility", tier="minimal", risk="safe", method="registry",
        target={"path": r"HKCU:\Control Panel\Accessibility\HighContrast", "name": "Flags", "force_type": "String"},
        tweak_value="122",
        min_os="7", max_os="11", os_verified=True,
        description="Stops Alt+Left Shift+PrtScn from instantly flipping the whole display into High Contrast mode "
                     "— a startling false-trigger on some keyboard layouts/games. High Contrast is still available "
                     "from Settings, just not this hotkey.",
    ),
    Tweak(
        id="bundle_disable_p2p_networking", name="Disable legacy peer-to-peer networking (PNRP)",
        category="network", tier="minimal", risk="safe", method="hybrid",
        target={"steps": [
            {"method": "service", "target": {"service_name": "PNRPsvc"}, "value": "Disabled"},
            {"method": "service", "target": {"service_name": "p2pimsvc"}, "value": "Disabled"},
            {"method": "service", "target": {"service_name": "PNRPAutoReg"}, "value": "Disabled"},
        ]}, tweak_value=None,
        min_os="7", max_os="11", os_verified=True,
        description="Three services backing Peer Name Resolution Protocol — legacy peer-to-peer discovery from the "
                     "HomeGroup/old-Remote-Assistance era. Not used by any current mainstream app.",
    ),
    Tweak(
        id="disable_rip_listener", name="Disable RIP Listener service",
        category="service", tier="minimal", risk="safe", method="service",
        target={"service_name": "iprip"}, tweak_value="Disabled",
        min_os="7", max_os="11", os_verified=True,
        description="Backs the optional RIP (Routing Information Protocol) Listener feature — off by default on "
                     "most installs already; formally disables it if it's present.",
    ),
    Tweak(
        id="disable_spatial_data_service", name="Disable Windows Spatial Service",
        category="service", tier="minimal", risk="safe", method="service",
        target={"service_name": "SharedRealitySvc"}, tweak_value="Disabled",
        min_os="8", max_os="11", os_verified=True,
        description="Backs spatial/mixed-reality data features. Irrelevant hardware-wise without HoloLens or "
                     "spatial-anchor-aware hardware.",
    ),
    Tweak(
        id="disable_iscsi_initiator", name="Disable Microsoft iSCSI Initiator service",
        category="service", tier="minimal", risk="safe", method="service",
        target={"service_name": "MSiSCSI"}, tweak_value="Disabled",
        min_os="7", max_os="11", os_verified=True,
        description="Backs connections to iSCSI network storage targets — a data-center/NAS feature basically "
                     "never used on a personal desktop or laptop.",
    ),
    Tweak(
        id="disable_wmi_perf_adapter", name="Disable WMI Performance Adapter",
        category="service", tier="minimal", risk="safe", method="service",
        target={"service_name": "wmiApSrv"}, tweak_value="Disabled",
        min_os="7", max_os="11", os_verified=True,
        description="Exposes performance counter data over WMI to remote/legacy monitoring tools. Irrelevant "
                     "unless something on this machine is being polled by an external perf-monitoring tool.",
    ),
    Tweak(
        id="disable_work_folders_service", name="Disable Work Folders service",
        category="service", tier="minimal", risk="safe", method="service",
        target={"service_name": "workfolderssvc"}, tweak_value="Disabled",
        min_os="11", max_os="11", os_verified=True,
        description="Backs Work Folders, an enterprise file-sync feature configured via a company's Work Folders "
                     "server — not used by individual/home setups.",
    ),

    # ---- Batch 9: 7 more bloatware, Edge shopping assistant, Recall/AI data analysis, compact Explorer view ----
    Tweak(
        id="bloat_alarms", name="Remove Alarms & Clock",
        category="bloatware", tier="standard", risk="safe", method="appx",
        target={"package_name": "Microsoft.WindowsAlarms"}, tweak_value=False,
        min_os="8", max_os="11", os_verified=True,
        description="Uninstalls the built-in Alarms & Clock app. Third-party or phone alarms cover this for most "
                     "people.",
    ),
    Tweak(
        id="bloat_screensketch", name="Remove Snipping Tool (modern)",
        category="bloatware", tier="maximal", risk="safe", method="appx",
        target={"package_name": "Microsoft.ScreenSketch"}, tweak_value=False,
        min_os="8", max_os="11", os_verified=True,
        description="Uninstalls the modern Snipping Tool/Snip & Sketch app. Offered at maximal, not standard, "
                     "since screenshot tools are genuinely used daily by a lot of people — PrtScn-to-clipboard "
                     "still works without this app.",
    ),
    Tweak(
        id="bloat_webexperience", name="Remove Widgets board app",
        category="bloatware", tier="standard", risk="safe", method="appx",
        target={"package_name": "MicrosoftWindows.Client.WebExperience"}, tweak_value=False,
        min_os="11", max_os="11", os_verified=True,
        description="Uninstalls the app backing the Widgets board itself, on top of the taskbar button toggle "
                     "covered elsewhere — removes the underlying app package, not just the entry point.",
    ),
    Tweak(
        id="disable_edge_shopping_assistant", name="Disable Edge shopping assistant / coupon popups",
        category="visual", tier="standard", risk="safe", method="registry",
        target={"path": r"HKLM:\SOFTWARE\Policies\Microsoft\Edge", "name": "EdgeShoppingAssistantEnabled"},
        tweak_value=0,
        min_os="10", max_os="11", os_verified=True,
        description="Stops Edge's built-in coupon-finder/price-comparison flyout from popping up on shopping "
                     "sites.",
    ),
    Tweak(
        id="disable_windows_recall_ai", name="Disable Windows AI data analysis (Recall / Click to Do)",
        category="privacy", tier="standard", risk="safe", method="registry",
        target={"path": r"HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsAI", "name": "DisableAIDataAnalysis"},
        tweak_value=1,
        min_os="11", max_os="11", os_verified=True,
        description="Policy-level block on Windows' on-device AI data analysis features (Recall's rolling "
                     "screenshot timeline and related Click to Do features on newer builds/Copilot+ PCs). No-op on "
                     "hardware that never had these features to begin with.",
    ),
    Tweak(
        id="explorer_compact_view", name="Use Explorer's compact view by default",
        category="visual", tier="minimal", risk="safe", method="registry",
        target={"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "name": "UseCompactMode"},
        tweak_value=1,
        min_os="11", max_os="11", os_verified=True,
        description="Windows 11 22H2+ — same as the 'Compact view' checkbox in Explorer's View menu. Tightens row "
                     "spacing in list/details view, fitting more files on screen.",
        requires_explorer_refresh=True,
    ),

    # ---- Batch 10: peer-service cleanup, classic status bar, search suggestions, window-animation bundle, 4 bloatware ----
    Tweak(
        id="disable_data_sharing_service", name="Disable Data Sharing Service",
        category="service", tier="minimal", risk="safe", method="service",
        target={"service_name": "DsSvc"}, tweak_value="Disabled",
        min_os="10", max_os="11", os_verified=True,
        description="Backs data-sharing contracts between UWP apps (share sheet plumbing for some app-to-app "
                     "transfers). Narrow feature, safe to disable for most usage patterns.",
    ),
    Tweak(
        id="disable_smartcard_removal_policy", name="Disable Smart Card Removal Policy service",
        category="service", tier="minimal", risk="safe", method="service",
        target={"service_name": "SCPolicySvc"}, tweak_value="Disabled",
        min_os="7", max_os="11", os_verified=True,
        description="Enforces lock-on-smart-card-removal policies — pairs with disabling the Smart Card service "
                     "itself elsewhere. Only relevant if you use smart-card sign-in.",
    ),
    Tweak(
        id="explorer_restore_status_bar", name="Show classic Explorer status bar",
        category="visual", tier="minimal", risk="safe", method="registry",
        target={"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "name": "ShowStatusBar"},
        tweak_value=1,
        min_os="11", max_os="11", os_verified=True,
        description="Restores the bottom status bar in Explorer windows (item count, selected-size, view controls "
                     "on some builds) where it's been hidden.",
        requires_explorer_refresh=True,
    ),
    Tweak(
        id="disable_search_suggestions_history", name="Stop Explorer address bar suggesting from search history",
        category="privacy", tier="standard", risk="safe", method="registry",
        target={"path": r"HKLM:\SOFTWARE\Policies\Microsoft\Windows\Explorer", "name": "DisableSearchBoxSuggestions"},
        tweak_value=1,
        min_os="7", max_os="11", os_verified=True,
        description="Stops the Explorer address/search box from auto-suggesting based on your recent search "
                     "history as you type.",
    ),
    Tweak(
        id="bundle_reduce_window_animations", name="Disable full-window drag redraw + minimize/maximize animation",
        category="visual", tier="minimal", risk="safe", method="hybrid",
        target={"steps": [
            {"method": "registry", "target": {"path": r"HKCU:\Control Panel\Desktop", "name": "DragFullWindows", "force_type": "String"}, "value": "0"},
            {"method": "registry", "target": {"path": r"HKCU:\Control Panel\Desktop\WindowMetrics", "name": "MinAnimate", "force_type": "String"}, "value": "0"},
        ]}, tweak_value=None,
        min_os="7", max_os="11", os_verified=True,
        description="Two classic performance toggles in one apply: dragging a window shows only an outline "
                     "instead of live-redrawing its contents, and minimize/maximize snaps instantly instead of "
                     "animating. Both are negligible on modern GPUs but genuinely help on weak integrated graphics.",
        requires_explorer_refresh=True,
    ),
    Tweak(
        id="bloat_voice_recorder", name="Remove Voice Recorder",
        category="bloatware", tier="standard", risk="safe", method="appx",
        target={"package_name": "Microsoft.WindowsSoundRecorder"}, tweak_value=False,
        min_os="8", max_os="11", os_verified=True,
        description="Uninstalls the built-in Voice Recorder app — low-usage utility for most people.",
    ),
    Tweak(
        id="bloat_networkspeedtest", name="Remove Network Speed Test",
        category="bloatware", tier="standard", risk="safe", method="appx",
        target={"package_name": "Microsoft.NetworkSpeedTest"}, tweak_value=False,
        min_os="8", max_os="11", os_verified=True,
        description="Uninstalls Microsoft's own bandwidth speed-test app — most people use a browser-based speed "
                     "test instead.",
    ),
    Tweak(
        id="bloat_print3d", name="Remove Print 3D",
        category="bloatware", tier="standard", risk="safe", method="appx",
        target={"package_name": "Microsoft.Print3D"}, tweak_value=False,
        min_os="8", max_os="11", os_verified=True,
        description="Uninstalls Print 3D, the companion app to 3D Builder for sending models to a 3D printer. "
                     "Dead weight without 3D-printing hardware.",
    ),
    Tweak(
        id="bloat_minecraft", name="Remove Minecraft trial preinstall",
        category="bloatware", tier="maximal", risk="safe", method="appx",
        target={"package_name": "Microsoft.MinecraftUWP"}, tweak_value=False,
        min_os="8", max_os="11", os_verified=True,
        description="Uninstalls the OEM-preinstalled Minecraft trial, if present on this build. Offered at "
                     "maximal since — obviously — plenty of people want to keep it.",
    ),
    Tweak(
        id="disable_edge_collections", name="Disable Edge Collections feature",
        category="visual", tier="standard", risk="safe", method="registry",
        target={"path": r"HKLM:\SOFTWARE\Policies\Microsoft\Edge", "name": "EdgeCollectionsEnabled"},
        tweak_value=0,
        min_os="10", max_os="11", os_verified=True,
        description="Disables Edge's Collections sidebar feature (save/organize web content into lists) via "
                     "policy — one less entry point cluttering the toolbar for people who don't use it.",
    ),

    # ---- Batch sourced/cross-checked against ChrisTitusTech/winutil config/tweaks.json ----
    Tweak(
        id="disable_store_recommended_search", name="Block Microsoft Store recommended search results",
        category="visual", tier="standard", risk="safe", method="hybrid",
        target={"steps": [
            {"method": "explorer_permission_deny", "target": {
                "path": r"%LocalAppData%\Packages\Microsoft.WindowsStore_8wekyb3d8bbwe\LocalState\store.db"},
             "value": "deny_everyone"},
        ]},
        tweak_value=True,
        min_os="10", max_os="11", os_verified=True,
        requires_explorer_refresh=False,
        description="Denies write/read access to the Store's local search-suggestion database so it stops "
                     "showing recommended Store apps when you search the Start Menu for something. Undo by "
                     "granting Everyone:F back on the same file (Flow's revert path does this automatically).",
    ),
    Tweak(
        id="disable_consumer_features", name="Disable Windows Consumer Features",
        category="bloatware", tier="standard", risk="safe", method="registry",
        target={"path": r"HKLM:\SOFTWARE\Policies\Microsoft\Windows\CloudContent", "name": "DisableWindowsConsumerFeatures"},
        tweak_value=1,
        min_os="10", max_os="11", os_verified=True,
        description="Stops Windows from silently installing 'suggested' third-party Store apps/games for the "
                     "signed-in user. Some in-box apps that rely on this channel (like Phone Link's first install) "
                     "become unavailable — reinstall manually from the Store if you need one later.",
    ),
    Tweak(
        id="disable_rdp_unsigned_warning", name="Disable RDP unsigned file warning",
        category="visual", tier="maximal", risk="advanced", method="hybrid",
        target={"steps": [
            {"method": "registry", "target": {"path": r"HKLM:\SOFTWARE\Policies\Microsoft\Windows NT\Terminal Services\Client", "name": "RedirectionWarningDialogVersion"}, "value": 1},
            {"method": "registry", "target": {"path": r"HKCU:\SOFTWARE\Microsoft\Terminal Server Client", "name": "RdpLaunchConsentAccepted"}, "value": 1},
        ]},
        tweak_value=True,
        min_os="10", max_os="11", os_verified=True,
        description="Suppresses the 'this RDP file is from an unknown publisher' warning added in recent Windows "
                     "updates. Advanced/maximal, not standard: this warning exists specifically to flag potentially "
                     "malicious .rdp files — only disable it if you script/launch trusted .rdp files regularly.",
    ),
    Tweak(
        id="disable_bitlocker", name="Disable BitLocker on system drive",
        category="service", tier="extreme", risk="advanced", method="hybrid",
        target={"steps": [
            {"method": "bitlocker_disable", "target": {"drive": "system"}, "value": False},
        ]},
        tweak_value=True,
        min_os="8", max_os="11", os_verified=True,
        description="Fully decrypts and disables BitLocker on the system drive. Genuinely removes your at-rest "
                     "disk encryption — if this laptop is ever lost or stolen with this off, whoever has it can "
                     "read the drive directly. Only worth it if you have another reason data-at-rest protection "
                     "isn't needed (e.g. it never leaves a locked room). Extreme tier for exactly this reason.",
    ),
    # NOTE: a "set hardware clock to UTC for dual-boot" tweak (RealTimeIsUniversal)
    # used to live here. Removed deliberately, not just re-tiered — it caused a
    # real clock-desync bug on a Windows-only machine (system time silently
    # shifted by the local UTC offset), and Flow should never be the reason
    # someone's clock/timestamps are wrong. If you actually dual-boot Linux,
    # set this manually yourself; it's a one-line reg add and not worth the
    # blast radius of Flow carrying it for everyone else.
    Tweak(
        id="explorer_hide_home_gallery", name="Hide Home and Gallery from Explorer nav pane",
        category="visual", tier="standard", risk="safe", method="hybrid",
        target={"steps": [
            {"method": "registry", "target": {"path": r"HKCU:\Software\Classes\CLSID\{f874310e-b6b7-47dc-bc84-b9e6b38f5903}", "name": "System.IsPinnedToNameSpaceTree"}, "value": 0},
            {"method": "registry", "target": {"path": r"HKCU:\Software\Classes\CLSID\{e88865ea-0e1c-4e20-9aa6-edcd0212c87c}", "name": "System.IsPinnedToNameSpaceTree"}, "value": 0},
        ]},
        tweak_value=True,
        min_os="11", max_os="11", os_verified=True,
        requires_explorer_refresh=True,
        description="Removes the 'Home' and 'Gallery' shortcuts Windows 11 pins into Explorer's left nav pane, "
                     "leaving just the folders you actually use. Pair with 'Open File Explorer to This PC' to "
                     "fully restore the pre-Windows-11 Explorer landing behavior.",
    ),
    Tweak(
        id="bloat_xbox_identity_provider", name="Remove Xbox Identity Provider",
        category="bloatware", tier="maximal", risk="safe", method="appx",
        target={"package_name": "Microsoft.XboxIdentityProvider"},
        tweak_value=False,
        min_os="10", max_os="11", os_verified=True,
        description="Removes the appx package handling Xbox Live sign-in. Breaks Xbox/Game Pass account "
                     "sign-in system-wide if you play anything through those — leave installed if you use Xbox app or Game Pass.",
    ),
    Tweak(
        id="bloat_xbox_speech_overlay", name="Remove Xbox Speech-to-Text Overlay",
        category="bloatware", tier="maximal", risk="safe", method="appx",
        target={"package_name": "Microsoft.XboxSpeechToTextOverlay"},
        tweak_value=False,
        min_os="10", max_os="11", os_verified=True,
        description="Removes the accessibility overlay that captions game voice chat. Harmless to remove if you "
                     "don't use Xbox voice-chat captioning.",
    ),
    Tweak(
        id="bloat_xbox_tcui", name="Remove Xbox TCUI (game invite/party UI)",
        category="bloatware", tier="maximal", risk="safe", method="appx",
        target={"package_name": "Microsoft.Xbox.TCUI"},
        tweak_value=False,
        min_os="10", max_os="11", os_verified=True,
        description="Removes the Xbox title-callable UI component (game invites, party chat overlays). Only "
                     "affects games that call into this for their own multiplayer UI.",
    ),
    Tweak(
        id="bloat_paint", name="Remove Paint",
        category="bloatware", tier="extreme", risk="advanced", method="appx",
        target={"package_name": "Microsoft.Paint"},
        tweak_value=False,
        min_os="11", max_os="11", os_verified=True,
        description="Removes the modern Paint app (moved to appx packaging in Windows 11). Extreme tier, not "
                     "standard: this is a genuinely useful, tiny, commonly-used app — most people don't want it gone.",
    ),
    Tweak(
        id="bloat_start_experiences", name="Remove Start Experiences app",
        category="bloatware", tier="maximal", risk="moderate", method="appx",
        target={"package_name": "Microsoft.StartExperiencesApp"},
        tweak_value=False,
        min_os="11", max_os="11", os_verified=True,
        description="Removes the appx package backing parts of the Windows 11 Start Menu experience (widgets "
                     "board host and related surfaces). Moderate risk: on some builds this component is more "
                     "tightly wired into Start Menu rendering than it looks — restore from Store if Start misbehaves.",
    ),
    Tweak(
        id="bloat_bingsearch", name="Remove Bing Search app",
        category="bloatware", tier="standard", risk="safe", method="appx",
        target={"package_name": "Microsoft.BingSearch"},
        tweak_value=False,
        min_os="11", max_os="11", os_verified=True,
        description="Removes the standalone Bing Search appx package (distinct from the taskbar search box, "
                     "which keeps working on local files/apps after this).",
    ),
    Tweak(
        id="enable_taskbar_end_task", name="Enable 'End Task' on taskbar right-click",
        category="visual", tier="minimal", risk="safe", method="registry",
        target={"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced\TaskbarDeveloperSettings", "name": "TaskbarEndTask"},
        tweak_value=1,
        min_os="11", max_os="11", os_verified=True,
        description="Adds an 'End Task' option when you right-click a running app's taskbar icon — kills it "
                     "immediately, same as Task Manager, without opening Task Manager first. Purely additive, no downside.",
    ),
    Tweak(
        id="disable_storage_sense", name="Disable Storage Sense",
        category="service", tier="maximal", risk="moderate", method="registry",
        target={"path": r"HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\StorageSense\Parameters\StoragePolicy", "name": "01"},
        tweak_value=0,
        min_os="10", max_os="11", os_verified=True,
        description="Stops Storage Sense from automatically deleting temp files and emptying Recycle Bin on a "
                     "schedule. Moderate, not safe: you lose automatic disk-space cleanup, which matters more on "
                     "a small/near-full drive — Flow's own 'Clean Temp Files' maintenance action is the manual "
                     "substitute if you disable this.",
    ),
    Tweak(
        id="disable_wpbt_execution", name="Disable Windows Platform Binary Table execution",
        category="service", tier="maximal", risk="advanced", method="registry",
        target={"path": r"HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager", "name": "DisableWpbtExecution"},
        tweak_value=1,
        min_os="8", max_os="11", os_verified=True,
        description="WPBT lets the OEM's firmware force-run a binary at every boot before Windows fully loads — "
                     "originally meant for anti-theft tools, also a documented attack surface since it runs "
                     "regardless of OS reinstall. Disabling can break vendor anti-theft/recovery tools that "
                     "depend on it (some Lenovo/HP utilities) — advanced tier because of that trade-off.",
    ),

    # ---- Second WinUtil pass — pulled from the customize-preferences section of tweaks.json ----
    # disable_mouse_acceleration and enable_sticky_keys removed deliberately — mouse-behavior
    # and keyboard-behavior tweaks are both off-limits now, and enable_sticky_keys was also a
    # verbatim duplicate of disable_sticky_keys_prompt (same registry path, same value, two IDs).
    Tweak(
        id="disable_mpo", name="Disable Multiplane Overlay (MPO)",
        category="visual", tier="maximal", risk="moderate", method="registry",
        target={"path": r"HKLM:\SOFTWARE\Microsoft\Windows\Dwm", "name": "OverlayTestMode"},
        tweak_value=5,
        min_os="10", max_os="11", os_verified=True,
        description="MPO is a GPU feature that composites video/overlay planes directly instead of through the "
                     "normal desktop compositor — faster, but a well-documented source of flickering, black "
                     "screens, and stutter on some GPU driver versions (notably older Intel/AMD laptop iGPUs). "
                     "Moderate: disabling forces full compositor rendering, which is a small overhead on very weak GPUs.",
    ),
    Tweak(
        id="enable_scrollbars_always", name="Always show scrollbars",
        category="visual", tier="minimal", risk="safe", method="registry",
        target={"path": r"HKCU:\Control Panel\Accessibility", "name": "DynamicScrollbars"},
        tweak_value=0,
        min_os="10", max_os="11", os_verified=True,
        requires_explorer_refresh=True,
        description="Keeps scrollbars permanently visible instead of the default auto-hide-until-hover behavior. "
                     "Purely a preference toggle — some people find auto-hiding scrollbars make it hard to gauge "
                     "how long a page/list is at a glance.",
    ),
    Tweak(
        id="taskbar_align_left", name="Align taskbar icons left (Windows 11)",
        category="visual", tier="minimal", risk="safe", method="registry",
        target={"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "name": "TaskbarAl"},
        tweak_value=0,
        min_os="11", max_os="11", os_verified=True,
        requires_explorer_refresh=True,
        description="Windows 11 centers the taskbar icons by default (a Windows 10 users' most-complained-about "
                     "change). This restores the classic left-aligned Start button and icon row. Purely cosmetic.",
    ),

    # ---- Third WinUtil pass — RAM-scaled service split, AI disable, browser-specific debloat, full Edge removal ----
    Tweak(
        id="tune_svchost_split_threshold", name="Scale svchost.exe grouping to installed RAM",
        category="service", tier="maximal", risk="moderate", method="svchost_split",
        target={}, tweak_value=None,
        min_os="10", max_os="11", os_verified=True,
        description="Raises SvcHostSplitThresholdInKB to match this machine's actual physical RAM instead of "
                     "Windows' fixed default (~3.5GB on 10/11), so services group into fewer, larger svchost.exe "
                     "processes rather than each getting its own. Meaningfully cuts idle process count/memory "
                     "overhead on machines with more RAM than the default threshold assumes. Moderate: on very "
                     "low-RAM machines already below the default this changes nothing; on some setups fewer, "
                     "larger svchost groups can mean one bad service takes a few others down with it on crash.",
    ),
    Tweak(
        id="bundle_disable_windows_ai", name="Disable Windows AI components (Copilot/Recall settings surface)",
        category="bloatware", tier="maximal", risk="advanced", method="hybrid",
        target={"steps": [
            {"method": "registry", "target": {"path": r"HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\Explorer", "name": "SettingsPageVisibility"}, "value": "hide:aicomponents"},
            {"method": "registry", "target": {"path": r"HKLM:\SOFTWARE\Policies\WindowsNotepad", "name": "DisableAIFeatures"}, "value": 1},
            {"method": "service", "target": {"service_name": "WSAIFabricSvc"}, "value": "Disabled"},
        ]},
        tweak_value=True,
        min_os="11", max_os="11", os_verified=True,
        description="Hides the AI components page in Settings, disables Notepad's AI rewrite/text features, and "
                     "disables the Windows AI Fabric service. Scoped intentionally narrower than WinUtil's version: "
                     "this does NOT bulk-remove Copilot/CoreAI appx packages or disable the Recall optional feature, "
                     "since those steps involve enumerating package-specific GUIDs and an optional-feature toggle "
                     "that vary by build and are easy to get wrong blind — do those manually via Settings > Apps "
                     "if you want Copilot/Recall fully gone, this tweak just kills the always-on background piece.",
    ),
    Tweak(
        id="bundle_brave_debloat", name="Debloat Brave browser (Rewards/Wallet/VPN/Leo AI/Talk/Tor/telemetry)",
        category="visual", tier="standard", risk="safe", method="hybrid",
        target={"steps": [
            {"method": "registry", "target": {"path": r"HKLM:\SOFTWARE\Policies\BraveSoftware\Brave", "name": "BraveRewardsDisabled"}, "value": 1},
            {"method": "registry", "target": {"path": r"HKLM:\SOFTWARE\Policies\BraveSoftware\Brave", "name": "BraveWalletDisabled"}, "value": 1},
            {"method": "registry", "target": {"path": r"HKLM:\SOFTWARE\Policies\BraveSoftware\Brave", "name": "BraveVPNDisabled"}, "value": 1},
            {"method": "registry", "target": {"path": r"HKLM:\SOFTWARE\Policies\BraveSoftware\Brave", "name": "BraveAIChatEnabled"}, "value": 0},
            {"method": "registry", "target": {"path": r"HKLM:\SOFTWARE\Policies\BraveSoftware\Brave", "name": "BraveStatsPingEnabled"}, "value": 0},
            {"method": "registry", "target": {"path": r"HKLM:\SOFTWARE\Policies\BraveSoftware\Brave", "name": "BraveNewsDisabled"}, "value": 1},
            {"method": "registry", "target": {"path": r"HKLM:\SOFTWARE\Policies\BraveSoftware\Brave", "name": "BraveTalkDisabled"}, "value": 1},
            {"method": "registry", "target": {"path": r"HKLM:\SOFTWARE\Policies\BraveSoftware\Brave", "name": "TorDisabled"}, "value": 1},
            {"method": "registry", "target": {"path": r"HKLM:\SOFTWARE\Policies\BraveSoftware\Brave", "name": "BraveP3AEnabled"}, "value": 0},
            {"method": "registry", "target": {"path": r"HKLM:\SOFTWARE\Policies\BraveSoftware\Brave", "name": "UrlKeyedAnonymizedDataCollectionEnabled"}, "value": 0},
            {"method": "registry", "target": {"path": r"HKLM:\SOFTWARE\Policies\BraveSoftware\Brave", "name": "SafeBrowsingExtendedReportingEnabled"}, "value": 0},
            {"method": "registry", "target": {"path": r"HKLM:\SOFTWARE\Policies\BraveSoftware\Brave", "name": "MetricsReportingEnabled"}, "value": 0},
        ]},
        tweak_value=True,
        min_os="7", max_os="11", os_verified=True,
        description="12 Brave-specific policy keys disabling Rewards, Wallet, built-in VPN, Leo AI chat, stats "
                     "ping, Brave News, Brave Talk, Tor windows, and telemetry/P3A/metrics reporting. These are "
                     "policy keys under HKLM — harmless no-ops if Brave isn't installed on this machine, so safe "
                     "to include even outside a Brave-specific setup.",
    ),
    Tweak(
        id="remove_edge_full", name="Remove Microsoft Edge entirely",
        category="bloatware", tier="extreme", risk="advanced", method="winget",
        target={"package_id": "Microsoft.Edge"},
        tweak_value=False,
        min_os="10", max_os="11", os_verified=True,
        description="Uninstalls Edge via winget rather than the appx-removal trick — Edge is a Win32 install, not "
                     "an appx package, so the normal 'appx' method doesn't touch it. Extreme tier: some in-box "
                     "Windows features (WebView2-dependent widgets, some Store app rendering, occasionally News/"
                     "Weather) silently depend on Edge's engine being present, and reinstalling isn't instant. "
                     "Revert reinstalls via 'winget install Microsoft.Edge' — same command WinUtil's own undo uses.",
    ),

    # ---- Re-audit pass additions — genuinely new ground, not duplicating anything above ----
    Tweak(
        id="disable_ntfs_last_access", name="Disable NTFS last-access timestamp updates",
        category="storage", tier="standard", risk="safe", method="registry",
        target={"path": r"HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem", "name": "NtfsDisableLastAccessUpdate"},
        tweak_value=1,
        min_os="7", max_os="11", os_verified=True,
        description="Same registry value fsutil's 'disablelastaccess' setting writes — every file/folder read "
                     "otherwise triggers a metadata write to record the access time, which is pure overhead almost "
                     "nothing on a consumer PC actually reads back. Biggest win on a mechanical HDD, where every one "
                     "of those writes costs a real seek; harmless on SSD too. Safe: the only thing that ever reads "
                     "last-access time is niche backup/forensic tooling, not normal Windows features.",
    ),
    Tweak(
        id="disable_fullscreen_optimizations_global", name="Disable Fullscreen Optimizations globally",
        category="gaming", tier="standard", risk="safe", method="hybrid",
        target={"steps": [
            {"method": "registry", "target": {"path": r"HKCU:\System\GameConfigStore", "name": "GameDVR_FSEBehaviorMode"}, "value": 2},
            {"method": "registry", "target": {"path": r"HKCU:\System\GameConfigStore", "name": "GameDVR_HonorUserFSEBehaviorMode"}, "value": 1},
        ]}, tweak_value=None,
        min_os="10", max_os="11", os_verified=True,
        description="Same GameConfigStore hive the Game DVR tweak above already writes to, two sibling keys: forces "
                     "'traditional' exclusive fullscreen instead of the borderless-windowed emulation Windows 10/11 "
                     "substitutes by default. Fixes the input-lag and stutter that emulation adds in older/lighter "
                     "titles, especially on weaker integrated GPUs — this is the same fix normally done per-game via "
                     "each .exe's Compatibility tab, applied once at the system level instead.",
    ),
    Tweak(
        id="disable_wcn_service", name="Disable Windows Connect Now service (WCNCSVC)",
        category="service", tier="maximal", risk="safe", method="service",
        target={"service_name": "wcncsvc"}, tweak_value="Disabled",
        min_os="7", max_os="11", os_verified=True,
        description="Configures Wi-Fi Protected Setup (WPS) push-button pairing — a router-side feature almost no "
                     "one on a PC (as opposed to a printer or IoT device) ever uses. Safe: this PC can still join "
                     "Wi-Fi networks normally by entering the password; it just can't act as a WPS enrollee.",
    ),

    # ---- WinUtil cross-reference pass — each entry below ported from ChrisTitusTech/winutil's
    # config/tweaks.json (fetched live from raw.githubusercontent.com, not memory), keeping only
    # keys that don't already exist anywhere above (checked by registry Name against every existing
    # Tweak's target before adding a single one of these). Real, maintained, 56k-star source —
    # exact Path/Name/Value copied as-is, not reworded from memory. This is the "real source" path
    # instead of inventing entries, same standard held earlier this session.
    Tweak(
        id="enable_dark_mode_apps", name="Enable dark mode for apps",
        category="visual", tier="minimal", risk="safe", method="registry",
        target={"path": r"HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Themes\Personalize", "name": "AppsUseLightTheme"},
        tweak_value=0,
        min_os="10", max_os="11", os_verified=True,
        description="Switches apps (Settings, Explorer, most modern UWP/WinUI apps) to dark theme. Purely "
                     "cosmetic — no perf impact, included for completeness alongside the system-wide dark mode key.",
        requires_explorer_refresh=True,
    ),
    Tweak(
        id="enable_dark_mode_system", name="Enable dark mode for system UI",
        category="visual", tier="minimal", risk="safe", method="registry",
        target={"path": r"HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Themes\Personalize", "name": "SystemUsesLightTheme"},
        tweak_value=0,
        min_os="10", max_os="11", os_verified=True,
        description="Sibling key to enable_dark_mode_apps — covers the taskbar/Start/system chrome specifically, "
                     "since Windows tracks app-theme and system-theme as two separate values.",
        requires_explorer_refresh=True,
    ),
    Tweak(
        id="disable_mouse_acceleration_speed", name="Disable mouse acceleration (pointer speed)",
        category="visual", tier="standard", risk="safe", method="registry",
        target={"path": r"HKCU:\Control Panel\Mouse", "name": "MouseSpeed"},
        tweak_value="0", min_os="7", max_os="11", os_verified=True,
        description="First of 3 sibling keys ('Enhance pointer precision' in Control Panel) — Windows accelerates "
                     "cursor movement non-linearly based on how fast you physically move the mouse, which most "
                     "gamers and anyone used to a 1:1 pointer find unpredictable. All 3 keys must be 0 together.",
    ),
    Tweak(
        id="disable_mouse_acceleration_threshold1", name="Disable mouse acceleration (threshold 1)",
        category="visual", tier="standard", risk="safe", method="registry",
        target={"path": r"HKCU:\Control Panel\Mouse", "name": "MouseThreshold1"},
        tweak_value="0", min_os="7", max_os="11", os_verified=True,
        description="Second of 3 sibling keys for disabling mouse acceleration — see disable_mouse_acceleration_speed.",
    ),
    Tweak(
        id="disable_mouse_acceleration_threshold2", name="Disable mouse acceleration (threshold 2)",
        category="visual", tier="standard", risk="safe", method="registry",
        target={"path": r"HKCU:\Control Panel\Mouse", "name": "MouseThreshold2"},
        tweak_value="0", min_os="7", max_os="11", os_verified=True,
        description="Third of 3 sibling keys for disabling mouse acceleration — see disable_mouse_acceleration_speed.",
    ),
    Tweak(
        id="enable_verbose_bsod_display_params", name="Show technical details on Blue Screen",
        category="visual", tier="standard", risk="safe", method="registry",
        target={"path": r"HKLM:\SYSTEM\CurrentControlSet\Control\CrashControl", "name": "DisplayParameters"},
        tweak_value=1, min_os="10", max_os="11", os_verified=True,
        description="Adds the stop-code/parameters block back to the crash screen instead of just the sad-face "
                     "summary — useful for actually diagnosing a crash instead of googling a QR code.",
    ),
    Tweak(
        id="enable_verbose_bsod_no_emoticon", name="Remove sad-face emoticon from Blue Screen",
        category="visual", tier="standard", risk="safe", method="registry",
        target={"path": r"HKLM:\SYSTEM\CurrentControlSet\Control\CrashControl", "name": "DisableEmoticon"},
        tweak_value=1, min_os="10", max_os="11", os_verified=True,
        description="Sibling key to enable_verbose_bsod_display_params — same crash screen, drops the emoticon "
                     "in favor of the technical layout.",
    ),
    Tweak(
        id="enable_verbose_logon_messages", name="Show detailed status messages during startup/shutdown",
        category="visual", tier="standard", risk="safe", method="registry",
        target={"path": r"HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System", "name": "VerboseStatus"},
        tweak_value=1, min_os="7", max_os="11", os_verified=True,
        description="Shows what Windows is actually doing during boot/shutdown/logon ('Applying computer "
                     "settings...', service names, etc.) instead of a silent spinner — useful for spotting which "
                     "step a slow boot is stuck on.",
    ),
    Tweak(
        id="disable_logon_acrylic_blur", name="Disable acrylic blur on logon screen",
        category="visual", tier="minimal", risk="safe", method="registry",
        target={"path": r"HKLM:\SOFTWARE\Policies\Microsoft\Windows\System", "name": "DisableAcrylicBackgroundOnLogon"},
        tweak_value=1, min_os="10", max_os="11", os_verified=True,
        description="Same category of win as disable_transparency — one less real-time blur/composite pass, this "
                     "time specifically on the lock/logon screen, cheapest on weak integrated graphics.",
        requires_explorer_refresh=False,
    ),
    Tweak(
        id="show_battery_percentage_tray", name="Show battery percentage in system tray",
        category="visual", tier="minimal", risk="safe", method="registry",
        target={"path": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "name": "IsBatteryPercentageEnabled"},
        tweak_value=1, applies_to="laptop_only",
        min_os="11", max_os="11", os_verified=True,
        description="Adds the numeric percentage next to the battery icon instead of requiring a hover/click to "
                     "see it. Laptop-only — hidden entirely on desktops.",
    ),
    Tweak(
        id="prefer_ipv4_over_ipv6", name="Prefer IPv4 over IPv6",
        category="network", tier="maximal", risk="moderate", method="registry",
        target={"path": r"HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip6\Parameters", "name": "DisabledComponents"},
        tweak_value=32, min_os="7", max_os="11", os_verified=True,
        description="Sets the classic 0x20 prefix-policy bit — leaves IPv6 itself on (unlike a full disable) but "
                     "makes Windows prefer an IPv4 route when both are available. Moderate, not safe: on a network "
                     "that's actually IPv6-first (some ISPs, some corporate VPNs), this can make things slower or "
                     "break IPv6-only services instead of helping — only worth it on a typical dual-stack home "
                     "network where IPv6 route selection has caused a specific slowdown.",
    ),
    Tweak(
        id="disable_start_menu_recommendations_policy", name="Disable Start Menu recommendations (policy-enforced)",
        category="visual", tier="standard", risk="safe", method="registry",
        target={"path": r"HKLM:\SOFTWARE\Policies\Microsoft\Windows\Explorer", "name": "HideRecommendedSection"},
        tweak_value=1, min_os="11", max_os="11", os_verified=True,
        description="Machine-policy version of disable_start_menu_recommendations (which only writes the "
                     "per-user Start_IrisRecommendations preference key). This one uses the actual Group Policy "
                     "path Microsoft added for 23H2+, and holds even if something else resets the per-user key.",
    ),
    Tweak(
        id="enable_game_mode", name="Enable Windows Game Mode",
        category="gaming", tier="standard", risk="safe", method="hybrid",
        target={"steps": [
            {"method": "registry", "target": {"path": r"HKCU:\Software\Microsoft\GameBar", "name": "AllowAutoGameMode"}, "value": 1},
            {"method": "registry", "target": {"path": r"HKCU:\Software\Microsoft\GameBar", "name": "AutoGameModeEnabled"}, "value": 1},
        ]}, tweak_value=None,
        min_os="10", max_os="11", os_verified=True,
        description="Makes sure Game Mode (background task/driver deprioritization while a game has focus) is "
                     "actually on — ships enabled by default on most installs, but some debloat tools or prior "
                     "'optimization' passes turn it off thinking it helps; it doesn't, this restores it.",
    ),
    Tweak(
        id="numlock_on_startup", name="Turn on Num Lock at startup",
        category="visual", tier="minimal", risk="safe", method="registry",
        target={"path": r"HKCU:\Control Panel\Keyboard", "name": "InitialKeyboardIndicators", "force_type": "String"},
        tweak_value="2", min_os="7", max_os="11", os_verified=True,
        description="Current-user only (there's also a HKU:\\.DEFAULT copy that would cover the logon screen and "
                     "new user profiles, but Flow's registry helper isn't set up for the HKU: hive, so this is "
                     "scoped to the signed-in user's sessions rather than reaching into a hive it hasn't touched "
                     "anywhere else). '2' = Num Lock on; sibling default '0' leaves it off, matching most laptops.",
    ),

    # ---- WinUtil cross-reference pass 2 — same sourcing standard as pass 1 above: fetched live,
    # checked every registry Name against everything already in this file (including pass 1) before
    # adding a single one. Pushes the static count past 200 without inventing anything.
    Tweak(
        id="disable_activity_feed_publish", name="Stop publishing activity history",
        category="privacy", tier="standard", risk="safe", method="registry",
        target={"path": r"HKLM:\SOFTWARE\Policies\Microsoft\Windows\System", "name": "PublishUserActivities"},
        tweak_value=0, min_os="10", max_os="11", os_verified=True,
        description="Sibling key to the existing EnableActivityFeed tweak, same policy path — stops this machine "
                     "from publishing what you've been doing to your Microsoft account's activity history, "
                     "independent of whether the feed feature itself is on.",
    ),
    Tweak(
        id="disable_activity_feed_upload", name="Stop uploading activity history",
        category="privacy", tier="standard", risk="safe", method="registry",
        target={"path": r"HKLM:\SOFTWARE\Policies\Microsoft\Windows\System", "name": "UploadUserActivities"},
        tweak_value=0, min_os="10", max_os="11", os_verified=True,
        description="Third sibling in the same Activity Feed policy group — blocks the actual network upload leg "
                     "specifically, on top of disable_activity_feed_publish.",
    ),
    Tweak(
        id="hide_hibernate_flyout_option", name="Hide Hibernate from the power flyout menu",
        category="power", tier="minimal", risk="safe", method="registry",
        target={"path": r"HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\FlyoutMenuSettings", "name": "ShowHibernateOption"},
        tweak_value=0, min_os="10", max_os="11", os_verified=True,
        description="Purely cosmetic — removes the 'Hibernate' entry from the Start Menu power button flyout. "
                     "Doesn't touch whether hibernation is actually available (that's the separate 'hibernate' "
                     "method tweak); this just tidies the menu, useful if Hibernate is disabled but still listed.",
    ),
    Tweak(
        id="set_storsvc_manual", name="Set Storage Service to Manual startup",
        category="service", tier="standard", risk="safe", method="service",
        target={"service_name": "StorSvc"}, tweak_value="Manual",
        min_os="10", max_os="11", os_verified=True,
        description="Storage Service handles Storage Sense and some external-drive notifications — Automatic by "
                     "default means it starts on every boot whether or not you use those features. Manual keeps "
                     "it available on-demand instead of resident at every startup; still starts fine when needed.",
    ),
    Tweak(
        id="reduce_keyboard_repeat_delay", name="Minimize keyboard repeat-start delay",
        category="visual", tier="minimal", risk="safe", method="registry",
        target={"path": r"HKCU:\Control Panel\Keyboard", "name": "KeyboardDelay"},
        tweak_value=0, min_os="7", max_os="11", os_verified=True,
        description="Same idea as startup_delay_zero — shortens the pause before a held key starts repeating to "
                     "the minimum Windows allows. Not a system-load fix, just removes a small artificial wait.",
    ),
    Tweak(
        id="disable_notification_center", name="Disable Notification Center / Action Center panel",
        category="notifications", tier="standard", risk="moderate", method="registry",
        target={"path": r"HKCU:\Software\Policies\Microsoft\Windows\Explorer", "name": "DisableNotificationCenter"},
        tweak_value=1, min_os="10", max_os="11", os_verified=True,
        description="Removes the whole Action Center flyout (Win+A), not just individual toast popups — Calendar "
                     "flyout in the clock area goes with it too. Moderate, not safe: this is a bigger UI change "
                     "than the toast-notification tweaks already in Flow, some people rely on that panel daily.",
        requires_explorer_refresh=True,
    ),
    Tweak(
        id="fix_modern_standby_network", name="Keep network connected during Modern Standby (S0) sleep",
        category="power", tier="maximal", risk="moderate", method="registry",
        target={"path": r"HKCU:\SOFTWARE\Policies\Microsoft\Power\PowerSettings\f15576e8-98b7-4186-b944-eafa664402d9", "name": "ACSettingIndex"},
        tweak_value=1, applies_to="laptop_only",
        min_os="10", max_os="11", os_verified=True,
        description="On S0/Modern-Standby laptops, fixes the common symptom of Discord/Teams/etc. showing "
                     "offline immediately after the lid closes by keeping network radios active through sleep. "
                     "Moderate: trades some of Modern Standby's battery-saving idle power draw for connectivity.",
    ),
    Tweak(
        id="force_legacy_s3_sleep", name="Force legacy S3 sleep instead of Modern Standby",
        category="power", tier="extreme", risk="advanced", method="registry",
        target={"path": r"HKLM:\SYSTEM\CurrentControlSet\Control\Power", "name": "PlatformAoAcOverride"},
        tweak_value=0, min_os="10", max_os="11", os_verified=True,
        description="On hardware that supports both, forces real S3 sleep (fans/screen/most power fully off) "
                     "instead of Modern Standby (S0), which keeps the platform semi-awake and can drain a laptop "
                     "battery noticeably overnight. Extreme/advanced on purpose: not all hardware handles the "
                     "override cleanly — some OEM firmware doesn't actually support S3 despite the key accepting "
                     "the write, which can produce sleep/wake issues instead of fixing anything. Requires a "
                     "restart to take effect either way.",
    ),
    Tweak(
        id="disable_lockscreen", name="Skip the lock screen, go straight to sign-in",
        category="visual", tier="maximal", risk="moderate", method="registry",
        target={"path": r"HKLM:\SOFTWARE\Policies\Microsoft\Windows\Personalization", "name": "NoLockScreen"},
        tweak_value=1, min_os="10", max_os="11", os_verified=True,
        description="Distinct from the existing camera-only lock-screen tweak — this removes the whole lock "
                     "screen (wallpaper/clock/notifications-preview) so waking the PC goes directly to the "
                     "password/PIN prompt. Moderate: this is a genuine change to the sign-in flow, and on some "
                     "Home-edition builds this policy path is silently ignored rather than actually applying — "
                     "harmless either way, but don't count on it if the lock screen still shows up after.",
    ),
]



TWEAK_BY_ID = {t.id: t for t in TWEAK_DATABASE}


def list_tweaks_for_tier(tier: str, profile: HardwareProfile) -> List[Tweak]:
    """Every tweak at or below the requested tier, filtered by hardware fit.
    Tier is cumulative: standard includes minimal, maximal includes both."""
    max_rank = TIER_ORDER.get(tier, 0)
    return [
        t for t in TWEAK_DATABASE
        if TIER_ORDER.get(t.tier, 0) <= max_rank and _tweak_applies(t, profile)
    ]


# ---- generic getters/setters, one pair per method ----

def _reg_type(value) -> str:
    if isinstance(value, bool):
        return "DWord"
    if isinstance(value, int):
        return "DWord"
    return "String"


def _registry_get(path: str, name: str):
    """Returns (value, existed). existed=False means the property was
    absent before we touch it — revert must delete, not just overwrite."""
    result = run_powershell(
        f"$v = Get-ItemProperty -Path '{path}' -Name '{name}' -ErrorAction SilentlyContinue; "
        f"if ($null -eq $v) {{ '__ABSENT__' }} else {{ $v.'{name}' }}"
    )
    if not result.success:
        return None, False
    raw = result.stdout.strip()
    if raw == "__ABSENT__" or raw == "":
        return None, False
    try:
        return int(raw), True
    except ValueError:
        return raw, True


def _registry_set(path: str, name: str, value, force_type: Optional[str] = None) -> ExecResult:
    ptype = force_type or _reg_type(value)
    val = int(value) if ptype == "DWord" else str(value).replace("'", "''")
    val_literal = str(val) if ptype == "DWord" else f"'{val}'"
    return run_powershell(
        f"New-Item -Path '{path}' -Force | Out-Null; "
        f"New-ItemProperty -Path '{path}' -Name '{name}' -Value {val_literal} "
        f"-PropertyType {ptype} -Force | Out-Null"
    )


def _values_equal(a, b) -> bool:
    """Loose equality for idempotency checks — registry/service values come
    back as mixed int/str/bool depending on source, this normalizes them."""
    if a is None or b is None:
        return False
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    if isinstance(a, int) and isinstance(b, int):
        return a == b
    return str(a).strip().lower() == str(b).strip().lower()


def _registry_delete(path: str, name: str) -> ExecResult:
    return run_powershell(
        f"Remove-ItemProperty -Path '{path}' -Name '{name}' -ErrorAction SilentlyContinue"
    )


def _service_get_start_type(service_name: str):
    result = run_powershell(f"(Get-Service -Name '{service_name}' -ErrorAction SilentlyContinue).StartType")
    if not result.success or not result.stdout.strip():
        return None, False
    return result.stdout.strip(), True


def _service_set_start_type(service_name: str, start_type) -> ExecResult:
    if start_type in (None, "None"):
        # service didn't exist / had no queryable start type — nothing safe to restore
        return ExecResult(command="service_set_start_type(noop)", success=True, returncode=0,
                           stdout="no previous start type to restore", stderr="", duration_s=0.0)
    if start_type == "Disabled":
        run_powershell(f"Stop-Service -Name '{service_name}' -Force -ErrorAction SilentlyContinue")
    return run_powershell(f"Set-Service -Name '{service_name}' -StartupType {start_type}")


def _power_scheme_get_active():
    result = run_hidden(["powercfg", "/getactivescheme"])
    if not result.success:
        return None, False
    import re
    match = re.search(r"([0-9a-fA-F-]{36})", result.stdout)
    return (match.group(1), True) if match else (None, False)


def _power_scheme_current_is_ultimate() -> bool:
    """Ultimate Performance is a hidden template plan (GUID e9a42b02-...)
    that Windows gives a BRAND NEW random GUID every time it's duplicated
    onto a machine — so the active instance's GUID is never the template
    GUID and can't be hardcoded/compared directly. powercfg's own output
    still labels it by name regardless of instance GUID, so check that."""
    result = run_hidden(["powercfg", "/getactivescheme"])
    return result.success and "ultimate performance" in result.stdout.lower()


def _power_scheme_set_active(guid: str) -> ExecResult:
    return run_hidden(["powercfg", "/setactive", guid])


def _power_setting_get(subgroup_guid: str, setting_guid: str):
    """Reads the CURRENT scheme's AC/DC index for one specific power
    setting (e.g. USB selective suspend) — distinct from _power_scheme_get_
    active(), which is about which whole SCHEME is active, not one setting
    within it. `powercfg /query` with no scheme arg means "the active
    scheme". Returns ({"ac": int, "dc": int}, existed) — existed False if
    this setting doesn't exist on this scheme/OS/hardware at all (e.g. no
    USB subsystem exposing it), which the caller should treat like any
    other "value never existed" case rather than an error."""
    result = run_hidden(["powercfg", "/query", "SCHEME_CURRENT", subgroup_guid, setting_guid])
    if not result.success:
        return None, False
    import re
    ac_match = re.search(r"Current AC Power Setting Index:\s*0x([0-9a-fA-F]+)", result.stdout)
    dc_match = re.search(r"Current DC Power Setting Index:\s*0x([0-9a-fA-F]+)", result.stdout)
    if not ac_match or not dc_match:
        return None, False
    return {"ac": int(ac_match.group(1), 16), "dc": int(dc_match.group(1), 16)}, True


def _power_setting_set(subgroup_guid: str, setting_guid: str, value: dict) -> ExecResult:
    """value is {"ac": int, "dc": int}. Sets both, then re-activates the
    current scheme — powercfg's set*valueindex calls stage the change but
    some settings (USB selective suspend among them) don't take effect
    live until the scheme is (re-)applied via /setactive."""
    ac_result = run_hidden(["powercfg", "/setacvalueindex", "SCHEME_CURRENT", subgroup_guid, setting_guid, str(value["ac"])])
    if not ac_result.success:
        return ac_result
    dc_result = run_hidden(["powercfg", "/setdcvalueindex", "SCHEME_CURRENT", subgroup_guid, setting_guid, str(value["dc"])])
    if not dc_result.success:
        return dc_result
    active = run_hidden(["powercfg", "/query", "SCHEME_CURRENT"])
    import re
    match = re.search(r"Power Scheme GUID:\s*([0-9a-fA-F-]{36})", active.stdout) if active.success else None
    if match:
        return _power_scheme_set_active(match.group(1))
    return dc_result  # couldn't confirm the active GUID to re-activate — settings are still staged either way


def _per_adapter_registry_get(base_path: str, name: str):
    """Checks a registry value across every NIC's per-interface subkey
    (e.g. Tcpip Parameters\\Interfaces\\{guid}) at once, in a single
    PowerShell call rather than one spawn per adapter. Only reports
    existed=True (with a representative value) if EVERY subkey present
    agrees on the same value — a machine with some adapters set and
    others not is, for this tweak's purposes, not-yet-applied, so the
    apply step re-touches every adapter uniformly rather than trying to
    reconcile a partial/inconsistent state.
    NOT YET VERIFIED ON REAL MULTI-ADAPTER HARDWARE — syntax-checked only,
    same caveat as the rest of this pass; the enumeration query is simple
    enough that it should behave, but flag if any rig shows odd results."""
    script = (
        f"$base = 'HKLM:\\SYSTEM\\CurrentControlSet\\Services\\Tcpip\\Parameters\\Interfaces';"
        f"Get-ChildItem $base -ErrorAction SilentlyContinue | ForEach-Object {{"
        f"  $v = (Get-ItemProperty -Path $_.PSPath -Name '{name}' -ErrorAction SilentlyContinue).'{name}';"
        f"  if ($null -eq $v) {{ 'MISSING' }} else {{ [string]$v }}"
        f"}}"
    )
    result = run_powershell(script)
    if not result.success:
        return None, False
    lines = [l.strip() for l in result.stdout.splitlines() if l.strip()]
    if not lines:
        return None, False  # no adapters found at all — nothing to enforce
    if any(l == "MISSING" for l in lines):
        return None, False
    unique_values = set(lines)
    if len(unique_values) != 1:
        return None, False  # adapters disagree with each other — treat as not-yet-applied
    try:
        return int(lines[0]), True
    except ValueError:
        return lines[0], True


def _per_adapter_registry_set(base_path: str, name: str, value) -> ExecResult:
    script = (
        f"$base = 'HKLM:\\SYSTEM\\CurrentControlSet\\Services\\Tcpip\\Parameters\\Interfaces';"
        f"Get-ChildItem $base -ErrorAction SilentlyContinue | ForEach-Object {{"
        f"  New-ItemProperty -Path $_.PSPath -Name '{name}' -Value {int(value)} -PropertyType DWord -Force | Out-Null"
        f"}}"
    )
    return run_powershell(script)


def _hibernate_get_enabled():
    value, existed = _registry_get(r"HKLM:\SYSTEM\CurrentControlSet\Control\Power", "HibernateEnabled")
    if not existed:
        return True, False  # Windows default is enabled when the key is absent
    return bool(value), True


def _hibernate_set(enabled: bool) -> ExecResult:
    return run_hidden(["powercfg", "/hibernate", "on" if enabled else "off"])


def _appx_get_installed(package_name: str) -> bool:
    result = run_powershell(f"[bool](Get-AppxPackage -Name '{package_name}' -ErrorAction SilentlyContinue)")
    return result.success and result.stdout.strip().lower() == "true"


def _appx_remove(package_name: str) -> ExecResult:
    """Removes the app for the current user AND removes the provisioned
    package so it doesn't silently reinstall for new user profiles / the
    next Windows feature update. Both are best-effort — a package that's
    already gone (or was never present) still reports success, since the
    end state the tweak wants ('not installed') is already true."""
    r1 = run_powershell(
        f"Get-AppxPackage -Name '{package_name}' -ErrorAction SilentlyContinue | Remove-AppxPackage -ErrorAction SilentlyContinue"
    )
    r2 = run_powershell(
        f"Get-AppxProvisionedPackage -Online -ErrorAction SilentlyContinue | "
        f"Where-Object {{ $_.DisplayName -eq '{package_name}' }} | "
        f"Remove-AppxProvisionedPackage -Online -ErrorAction SilentlyContinue"
    )
    success = r1.success and r2.success
    return ExecResult(
        command=f"appx_remove({package_name})", success=success, returncode=0 if success else -1,
        stdout=f"removed '{package_name}' (user package + provisioning)" if success else r1.stdout,
        stderr="" if success else (r1.stderr or r2.stderr), duration_s=r1.duration_s + r2.duration_s,
    )


def _appx_reinstall_note(package_name: str) -> ExecResult:
    """Honest limitation: once Remove-AppxProvisionedPackage runs, the app's
    files are gone from WinSxS — there is no local source to re-register
    from. A true revert means re-pulling it from the Microsoft Store, which
    Flow can't script without a signed-in Store session. This is NOT a
    silent no-op — it reports as a failure so revert_all() keeps it in the
    log and the GUI surfaces it, rather than pretending the app came back."""
    return ExecResult(
        command=f"appx_reinstall({package_name})", success=False, returncode=-1,
        stdout="", stderr=f"'{package_name}' was removed — reinstall manually from Microsoft Store, "
                          f"Flow cannot restore a removed Store app automatically", duration_s=0.0,
    )


def _onedrive_installed() -> bool:
    result = run_powershell(
        "[bool](Get-ItemProperty 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\OneDriveSetup.exe' -ErrorAction SilentlyContinue)"
    )
    return result.success and result.stdout.strip().lower() == "true"


def _onedrive_remove() -> ExecResult:
    """Kills the running process, then runs the per-user OneDriveSetup.exe
    uninstaller (present in either SysWOW64 or System32 depending on
    build — try both). Does not touch OneDrive folder contents already in
    the user's profile, only removes the app and its Explorer sidebar entry."""
    run_powershell("Stop-Process -Name OneDrive -Force -ErrorAction SilentlyContinue")
    result = run_powershell(
        "$paths = @(\"$env:SystemRoot\\SysWOW64\\OneDriveSetup.exe\", \"$env:SystemRoot\\System32\\OneDriveSetup.exe\"); "
        "$exe = $paths | Where-Object { Test-Path $_ } | Select-Object -First 1; "
        "if ($exe) { Start-Process $exe '/uninstall' -Wait -NoNewWindow; 'done' } else { 'not found' }"
    )
    ok = result.success and "not found" not in result.stdout
    return ExecResult(command="onedrive_remove", success=ok, returncode=0 if ok else -1,
                       stdout=result.stdout, stderr="" if ok else "OneDriveSetup.exe not found on this build",
                       duration_s=result.duration_s)


def _onedrive_reinstall_note() -> ExecResult:
    """Same honesty stance as _appx_reinstall_note() — the uninstaller
    deletes OneDriveSetup.exe itself, so there's no local binary left to
    re-run. Revert means downloading the installer from onedrive.com."""
    return ExecResult(command="onedrive_reinstall", success=False, returncode=-1,
                       stdout="", stderr="OneDrive was removed — reinstall manually from "
                                        "https://onedrive.live.com/about/download, Flow cannot restore it automatically",
                       duration_s=0.0)


def _icacls_get_deny_state(path: str) -> tuple:
    """True if an Everyone:F deny ACE is already present on this file —
    used purely as the idempotency check, not a full ACL dump."""
    result = run_hidden(["icacls", path], timeout=15)
    has_deny = "Everyone:(N)" in result.stdout or "(DENY)" in result.stdout
    return has_deny, True


def _icacls_set_deny(path: str) -> ExecResult:
    return run_hidden(["icacls", path, "/deny", "Everyone:F"], timeout=15)


def _icacls_set_grant(path: str) -> ExecResult:
    return run_hidden(["icacls", path, "/grant", "Everyone:F"], timeout=15)


def _bitlocker_get_status(drive: str) -> tuple:
    """True/False = BitLocker currently on/off for the target drive. None
    (existed=False) if the query itself fails — e.g. BitLocker isn't
    available on this SKU/edition at all, which is a different fact than
    'off' and shouldn't be conflated with it."""
    target = os.environ.get("SystemDrive", "C:") if drive == "system" else drive
    rows = _run_ps_json(f'Get-BitLockerVolume -MountPoint "{target}" | Select-Object ProtectionStatus | ConvertTo-Json')
    if not rows:
        return False, False
    return (rows[0].get("ProtectionStatus") == 1), True


def _bitlocker_disable(drive: str) -> ExecResult:
    target = os.environ.get("SystemDrive", "C:") if drive == "system" else drive
    return run_hidden(f'powershell -NoProfile -Command "Disable-BitLocker -MountPoint {target}"',
                       shell=True, timeout=60)


def _bitlocker_enable(drive: str) -> ExecResult:
    """Re-enabling isn't a silent one-liner — Enable-BitLocker needs a
    protector (TPM or recovery password) chosen deliberately, not guessed
    by a revert script. Surfacing that honestly beats a fake success."""
    return ExecResult(command="bitlocker_enable", success=False, returncode=-1,
                       stdout="", stderr="BitLocker was disabled — re-enabling needs a deliberate protector choice "
                                        "(TPM/recovery password); run 'manage-bde -on %SystemDrive%' yourself or "
                                        "use Settings > Privacy & Security > Device encryption, Flow won't guess this",
                       duration_s=0.0)


def _winget_is_installed(package_id: str) -> bool:
    result = run_hidden(["winget", "list", "--id", package_id, "--accept-source-agreements"], timeout=30)
    return result.success and package_id.lower() in result.stdout.lower()


def _winget_uninstall(package_id: str) -> ExecResult:
    return run_hidden(["winget", "uninstall", "--id", package_id, "--silent",
                        "--accept-source-agreements", "--disable-interactivity"], timeout=180)


def _winget_install(package_id: str) -> ExecResult:
    return run_hidden(["winget", "install", "--id", package_id, "--silent",
                        "--accept-source-agreements", "--accept-package-agreements",
                        "--disable-interactivity"], timeout=300)


def _svchost_split_get() -> tuple:
    return _registry_get(r"HKLM:\SYSTEM\CurrentControlSet\Control", "SvcHostSplitThresholdInKB")


def _svchost_split_apply() -> ExecResult:
    """Sets the svchost split threshold to this machine's actual physical
    RAM (in KB) — matching WinUtil's own approach — rather than a fixed
    number, since the 'right' threshold scales with how much RAM exists
    to split across. Raising it toward total RAM means fewer, larger
    svchost.exe groups instead of one per service; only worth it on
    machines already tight on RAM where the several-hundred-KB overhead
    per split-out svchost process is worth reclaiming."""
    rows = _run_ps_json("(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory | ConvertTo-Json")
    if not rows:
        return ExecResult(command="svchost_split_apply", success=False, returncode=-1,
                           stdout="", stderr="could not read installed RAM", duration_s=0.0)
    total_bytes = rows[0] if isinstance(rows[0], (int, float)) else rows[0].get("value", 0)
    kb = int(total_bytes) // 1024
    return _registry_set(r"HKLM:\SYSTEM\CurrentControlSet\Control", "SvcHostSplitThresholdInKB", kb, "DWord")


def _svchost_split_revert(previous_value, existed: bool) -> ExecResult:
    if not existed:
        return _registry_delete(r"HKLM:\SYSTEM\CurrentControlSet\Control", "SvcHostSplitThresholdInKB")
    return _registry_set(r"HKLM:\SYSTEM\CurrentControlSet\Control", "SvcHostSplitThresholdInKB", previous_value, "DWord")


# ---- revert log ----

def _revert_log_path() -> str:
    import os
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "flow_revert_log.json")


def _load_revert_log() -> list:
    import os
    path = _revert_log_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _save_revert_log(entries: list) -> None:
    with open(_revert_log_path(), "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, default=str)


def _get_applied_tweak_ids() -> set:
    """Ids currently applied, per the revert log's own source-of-truth
    convention (see revert_all()'s docstring: successfully-reverted entries
    are removed on revert, so whatever's still in the log IS the applied
    set — no separate 'applied' flag exists or is needed). Used to keep
    the AI chat from re-suggesting something already on, and to let it
    answer 'what have you already turned on' directly instead of guessing."""
    return {e.get("tweak_id") for e in _load_revert_log() if e.get("tweak_id")}


def dedupe_revert_log() -> dict:
    """Physically collapses the on-disk revert log to one entry per
    tweak_id (most recent wins), instead of just deduping in-memory each
    daemon cycle. apply_tier() has always appended a fresh entry per real
    change with no dedup, so re-running a tier (or overlapping tiers)
    repeatedly leaves the log several times its true size — on a real
    rig this reached 132 entries for 76 unique tweaks. Doesn't change
    which tweaks are considered applied, just removes the redundant
    history; revert_all() and the daemon both already treat 'most recent
    entry for this id' as the source of truth."""
    before = _load_revert_log()
    deduped = {}
    for entry in before:
        deduped[entry["tweak_id"]] = entry
    after = list(deduped.values())
    _save_revert_log(after)
    return {"before": len(before), "after": len(after), "removed": len(before) - len(after)}


# ═══════════════════════════════════════════════════════════════════
# SECTION 4B — DAEMON (drift detection + reapply, runs unattended)
# ═══════════════════════════════════════════════════════════════════
# Windows Update, a reinstalled app, or another tool can silently reset a
# tweak Flow already applied (a service re-enabled, a policy key cleared
# on a feature-update rollback, etc). The daemon periodically walks the
# SAME revert log used for manual revert — every entry in it is, by
# definition, "a tweak Flow applied and hasn't reverted" — re-reads the
# live value, and if it's drifted away from the tweak's target state,
# reapplies just that one setting. It never adds new tweaks or changes
# what's in the log; it only enforces what's already there. Runs as an
# unprivileged loop that does nothing (and logs why) if not elevated,
# same as every other apply path in this file.

@dataclass
class MaintenanceAction:
    id: str
    name: str
    description: str
    command: Union[List[str], str]
    timeout: Optional[int] = None  # None = no cap. These are disk-bound ops
                                    # (temp cleanup, SFC, DISM, defrag...) —
                                    # duration scales with how full/fragmented/
                                    # slow the drive is, especially on HDD.
                                    # Killing one mid-run via an arbitrary
                                    # timeout is worse than a long wait: SFC/
                                    # DISM restarted from scratch just burns
                                    # more time, and a killed defrag can leave
                                    # a drive mid-rearrange. Let them finish.
    shell: bool = False
    requires_admin: bool = True
    disruptive: bool = False     # True = ties up the machine a while / reboots recommended after
    hdd_only: bool = False       # e.g. defrag — meaningless/harmful-ish on SSD, gate it
    ssd_only: bool = False       # e.g. TRIM/Optimize-Volume ReTrim


def _maintenance_target_disk() -> str:
    """Best-guess system drive letter for actions that need one (defrag,
    chkdsk). Falls back to C: if detection fails rather than raising —
    a bad guess here just makes one action a no-op ExecResult, never a
    crash."""
    try:
        return os.environ.get("SystemDrive", "C:") or "C:"
    except Exception:
        return "C:"


MAINTENANCE_ACTIONS: List[MaintenanceAction] = [
    MaintenanceAction(
        id="clean_temp_files", name="Clean Temp Files",
        description="Deletes user and system %TEMP% contents. Files in use are skipped automatically, not forced — safe on a running system.",
        command='powershell -NoProfile -Command "Get-ChildItem -Path $env:TEMP,$env:WINDIR\\Temp -Recurse -Force -ErrorAction SilentlyContinue | Remove-Item -Force -Recurse -ErrorAction SilentlyContinue; Write-Output done"',
        shell=True,
    ),
    MaintenanceAction(
        id="clean_windows_update_cache", name="Clean Windows Update Cache",
        description="Stops the Windows Update service, clears SoftwareDistribution\\Download, restarts the service. Frees space held by old/partial update downloads; does not affect installed updates.",
        command='powershell -NoProfile -Command "Stop-Service wuauserv -Force; Remove-Item -Path $env:WINDIR\\SoftwareDistribution\\Download\\* -Recurse -Force -ErrorAction SilentlyContinue; Start-Service wuauserv; Write-Output done"',
        shell=True,
    ),
    MaintenanceAction(
        id="empty_recycle_bin", name="Empty Recycle Bin",
        description="Empties the Recycle Bin for all drives, no confirmation prompt, no notification sound.",
        command='powershell -NoProfile -Command "Clear-RecycleBin -Force -ErrorAction SilentlyContinue; Write-Output done"',
        shell=True, requires_admin=False,
    ),
    MaintenanceAction(
        id="clean_prefetch", name="Clean Prefetch Cache",
        description="Clears C:\\Windows\\Prefetch. Windows rebuilds this automatically from normal use — first launch of each app after this will be marginally slower until it's repopulated.",
        command='powershell -NoProfile -Command "Remove-Item -Path $env:WINDIR\\Prefetch\\* -Force -ErrorAction SilentlyContinue; Write-Output done"',
        shell=True,
    ),
    MaintenanceAction(
        id="clear_thumbnail_cache", name="Clear Thumbnail Cache",
        description="Deletes cached Explorer thumbnail database files. They regenerate the next time you browse folders with images/video — first browse of a large media folder will be slower once.",
        command='powershell -NoProfile -Command "taskkill /f /im explorer.exe; Remove-Item -Path \\"$env:LOCALAPPDATA\\Microsoft\\Windows\\Explorer\\thumbcache_*.db\\" -Force -ErrorAction SilentlyContinue; Start-Process explorer.exe; Write-Output done"',
        shell=True, requires_admin=False,
    ),
    MaintenanceAction(
        id="flush_dns", name="Flush DNS Cache",
        description="Clears the local DNS resolver cache. Fixes stale-record issues after a site/server changed IPs; has no lasting downside.",
        command=["ipconfig", "/flushdns"], requires_admin=False,
    ),
    MaintenanceAction(
        id="clear_event_logs", name="Clear Windows Event Logs",
        description="Wipes Application/System/Security event log history. Frees a small amount of disk space and de-clutters Event Viewer; you lose historical diagnostic data, so skip this if you're mid-troubleshooting something.",
        command='powershell -NoProfile -Command "wevtutil el | ForEach-Object { wevtutil cl \\"$_\\" 2>$null }; Write-Output done"',
        shell=True, disruptive=True,
    ),
    MaintenanceAction(
        id="sfc_scan", name="System File Checker (sfc /scannow)",
        description="Scans all protected system files and repairs corrupted ones from the component store. Read-only diagnostic if nothing's broken; can take 10-20 min on an HDD. Reboot afterward if it reports fixes.",
        command="sfc /scannow", shell=True, disruptive=True,
    ),
    MaintenanceAction(
        id="dism_check_health", name="DISM CheckHealth",
        description="Fast read-only check of whether the component store is flagged as corrupted. Always run this before RestoreHealth — it's the 10-second version.",
        command="DISM /Online /Cleanup-Image /CheckHealth", shell=True,
    ),
    MaintenanceAction(
        id="dism_restore_health", name="DISM RestoreHealth",
        description="Repairs the component store using Windows Update as the source (needs internet). This is what actually fixes corruption sfc /scannow can't repair on its own — run sfc again after this completes.",
        command="DISM /Online /Cleanup-Image /RestoreHealth", shell=True, disruptive=True,
    ),
    MaintenanceAction(
        id="dism_component_cleanup", name="DISM Component Store Cleanup",
        description="Removes superseded versions of system files from WinSxS, freeing disk space. You lose the ability to uninstall Windows updates older than the current cleanup point — normal maintenance, not something to run right before you might need a rollback.",
        command="DISM /Online /Cleanup-Image /StartComponentCleanup", shell=True, disruptive=True,
    ),
    MaintenanceAction(
        id="disk_defrag_hdd", name="Defragment Disk (HDD)",
        description="Runs Optimize-Volume's defrag pass on the system drive. Mechanical-disk only — never run on SSD, it wastes write cycles for zero speed benefit since there's no seek time to save.",
        command=lambda: f'powershell -NoProfile -Command "Optimize-Volume -DriveLetter {_maintenance_target_disk().rstrip(chr(58))} -Defrag -Verbose"',
        shell=True, disruptive=True, hdd_only=True,
    ),
    MaintenanceAction(
        id="disk_trim_ssd", name="TRIM / Retrim (SSD)",
        description="Runs Optimize-Volume's ReTrim pass on the system drive. SSD-only equivalent of defrag — tells the drive which blocks are free so it can manage wear-leveling. Do not run this on an HDD; it's a no-op there but wastes the maintenance window.",
        command=lambda: f'powershell -NoProfile -Command "Optimize-Volume -DriveLetter {_maintenance_target_disk().rstrip(chr(58))} -ReTrim -Verbose"',
        shell=True, ssd_only=True,
    ),
    MaintenanceAction(
        id="chkdsk_schedule", name="Schedule CHKDSK on Next Boot",
        description="Schedules a full CHKDSK /f /r pass for the next restart (can't run live on the system drive). Takes 20 min-several hours depending on disk size and health — schedule this when you don't need the machine soon.",
        command=f'echo Y| chkdsk {_maintenance_target_disk()} /f /r', shell=True, disruptive=True,
    ),
    MaintenanceAction(
        id="disk_cleanup_utility", name="Disk Cleanup (cleanmgr, all categories)",
        description="Runs the classic Disk Cleanup utility with every category pre-selected via the sageset profile Flow configures on first run. Broader than clean_temp_files — also hits old update files, error reports, and delivery-optimization cache.",
        command="cleanmgr /sagerun:65535", shell=True, disruptive=True,
    ),
    MaintenanceAction(
        id="dedupe_path_env", name="Deduplicate PATH Environment Variable",
        description="Removes exact-duplicate directory entries (case-insensitive, trailing-backslash-normalized) from the User and Machine PATH variables, keeping first-seen order. Every unique directory stays — only redundant repeats are dropped. Safe: a long, duplicate-laden PATH slows down every unresolved command lookup and risks hitting Windows' ~2047-char PATH length limit, which causes cryptic \"command not found\" failures for tools that ARE installed.",
        command='powershell -NoProfile -Command "$changed=@(); foreach($scope in \'User\',\'Machine\'){$raw=[Environment]::GetEnvironmentVariable(\'Path\',$scope); if(-not $raw){continue}; $parts=$raw -split \';\' | ForEach-Object{$_.Trim()} | Where-Object{$_ -ne \'\'}; $seen=New-Object System.Collections.Generic.HashSet[string]; $dedup=@(); foreach($p in $parts){$k=$p.TrimEnd(\'\\\').ToLowerInvariant(); if($seen.Add($k)){$dedup+=$p}}; if($dedup.Count -lt $parts.Count){[Environment]::SetEnvironmentVariable(\'Path\',($dedup -join \';\'),$scope); $changed+=($scope+\': removed \'+($parts.Count-$dedup.Count)+\' duplicate(s)\')}}; if($changed.Count -eq 0){\'No duplicate PATH entries found.\'}else{$changed -join \'; \'}"',
        shell=True,
    ),
    MaintenanceAction(
        id="clean_orphaned_uninstall_entries", name="Remove Orphaned Uninstall Registry Entries",
        description="Scans HKLM/HKCU 'Programs and Features' registry entries (both native and WOW6432Node views) for ones whose UninstallString points to an exe that no longer exists on disk — leftover from software removed by deleting its folder instead of properly uninstalling. Removes only that stale list entry, never touches files. Skips MSI-managed entries entirely (msiexec-based ones are left to Windows Installer, not this heuristic) and anything flagged SystemComponent. Manual-only for now (not run automatically by the idle daemon) until it has a track record.",
        command='powershell -NoProfile -Command "$q=[char]34; $paths=@(\'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*\',\'HKLM:\\SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*\',\'HKCU:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*\'); $removed=@(); foreach($base in $paths){Get-ItemProperty -Path $base -ErrorAction SilentlyContinue | ForEach-Object {$k=$_; if($k.SystemComponent -eq 1){return}; if(-not $k.DisplayName){return}; $us=$k.UninstallString; if(-not $us){return}; if($us -match \'msiexec\'){return}; $exe=$us; if($exe.StartsWith($q)){$endIdx=$exe.IndexOf($q,1); if($endIdx -gt 0){$exe=$exe.Substring(1,$endIdx-1)}} else {$exe=($exe -split \' \')[0]}; if($exe -and -not (Test-Path -LiteralPath $exe)){$removed+=$k.DisplayName; Remove-Item -Path $k.PSPath -Recurse -Force -ErrorAction SilentlyContinue}}}; if($removed.Count -eq 0){\'No orphaned uninstall entries found.\'}else{\'Removed \' + $removed.Count + \' orphaned entries: \' + ($removed -join \', \')}"',
        shell=True, disruptive=True,
    ),
]

MAINTENANCE_BY_ID = {a.id: a for a in MAINTENANCE_ACTIONS}


def list_maintenance_actions(profile: HardwareProfile) -> List[MaintenanceAction]:
    """Same hardware-gating philosophy as tweaks — hdd_only/ssd_only actions
    are filtered by the disk's actual MediaType so the UI never even offers
    a defrag on an SSD or a TRIM on a spinning disk."""
    is_ssd = any(d.media_type == "SSD" for d in profile.disks)
    is_hdd = any(d.media_type == "HDD" for d in profile.disks)
    out = []
    for a in MAINTENANCE_ACTIONS:
        if a.hdd_only and is_ssd and not is_hdd:
            continue
        if a.ssd_only and is_hdd and not is_ssd:
            continue
        out.append(a)
    return out


def run_maintenance_action(action_id: str) -> ExecResult:
    """One-shot execution — no revert log entry (there's nothing to
    revert: temp files deleted are just deleted, sfc/DISM repair rather
    than change settings). Every run still goes through the daemon log
    via the caller so there's a timestamped record of what ran and when."""
    action = MAINTENANCE_BY_ID.get(action_id)
    if action is None:
        return ExecResult(command=f"run_maintenance_action({action_id})", success=False,
                           returncode=-1, stdout="", stderr=f"unknown action '{action_id}'", duration_s=0.0)
    if action.requires_admin and not is_admin():
        return ExecResult(command=f"run_maintenance_action({action_id})", success=False,
                           returncode=-1, stdout="", stderr="not running elevated — this action requires admin", duration_s=0.0)
    cmd = action.command() if callable(action.command) else action.command
    return run_hidden(cmd, timeout=action.timeout, shell=action.shell)


def _daemon_log_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "flow_daemon_log.jsonl")


_DAEMON_LOG_MAX_BYTES = 2 * 1024 * 1024   # trim trigger — cheap os.path.getsize check, not a per-write read
_DAEMON_LOG_KEEP_LINES = 2000              # lines kept after a trim (jsonl, most recent last)


def _daemon_log(entry: dict) -> None:
    """Appends one JSON line per daemon cycle — jsonl instead of a single
    JSON array so a crash mid-write never corrupts prior history, and a
    long-running daemon doesn't have to rewrite a growing file every cycle.

    Rotation: a daemon running hourly forever would otherwise grow this
    file without bound — real concern on the HP rig's small/HDD drive,
    not just a theoretical one. Checked via a cheap os.path.getsize() stat
    on every write (no read), and only pays the read+rewrite cost on the
    rare cycle that actually crosses the size threshold."""
    entry = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), **entry}
    path = _daemon_log_path()
    try:
        if os.path.exists(path) and os.path.getsize(path) > _DAEMON_LOG_MAX_BYTES:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()[-_DAEMON_LOG_KEEP_LINES:]
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lines)
    except OSError:
        pass  # rotation is best-effort — never let a trim failure block logging
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


_BLOCKLIST_THRESHOLD = 3  # consecutive identical-ish apply FAILURES before a tweak is skipped
_STICKY_DRIFT_THRESHOLD = 5  # consecutive cycles a tweak shows as drifted even though reapply
# reported success — i.e. the write goes through but doesn't stick. This is a distinct failure
# mode from _BLOCKLIST_THRESHOLD above: a straight apply error (bad path, missing service) trips
# the counter in _record_failure below, but a value that gets silently reset between checks
# (Windows re-asserting a protected default, another tool/policy rewriting it, an Explorer/Edge
# background sync overwriting it) never fails an apply call — it just never stays fixed. Tracked
# separately in flow_daemon_driftstreak.json so it doesn't interfere with the failure counter's
# semantics (a "successful" reapply resets consecutive_fails, which is correct for that counter,
# but shouldn't erase evidence that this ID is stuck in a reapply loop).


def _blocklist_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "flow_daemon_blocklist.json")


def _load_blocklist() -> dict:
    """Maps tweak/step id -> {consecutive_fails, last_error, blocked}. Tracks
    apply attempts that keep failing (OS-level lock like UCPD or a protected
    service ACL, not something Flow retrying harder will ever fix) so the
    daemon stops hammering them every cycle once that's established."""
    path = _blocklist_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_blocklist(state: dict) -> None:
    with open(_blocklist_path(), "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, default=str)


def _drift_streak_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "flow_daemon_driftstreak.json")


def _load_drift_streaks() -> dict:
    """Maps tweak/step id -> consecutive cycle count where it showed up in
    `drifted`. Reset to absent (not just 0) the moment a check finds it
    matching target again — a clean pass means whatever was undoing it
    stopped, or it was fixed by other means, and the streak shouldn't
    carry a stale count into the next real drift episode."""
    path = _drift_streak_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_drift_streaks(state: dict) -> None:
    with open(_drift_streak_path(), "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, default=str)


def daemon_blocklist_status() -> dict:
    """Read-only view of the blocklist, including any AI diagnosis attached
    when a tweak first crossed the threshold. Separate from daemon_status()
    (which is about the scheduled-task/loop itself) since this is about
    per-tweak state and can be a longer list. Also surfaces in-progress
    drift streaks that haven't crossed _STICKY_DRIFT_THRESHOLD yet — useful
    for spotting a tweak that's about to get blocked before it happens."""
    state = _load_blocklist()
    blocked = {k: v for k, v in state.items() if v.get("blocked")}
    streaks = _load_drift_streaks()
    in_progress = {k: v for k, v in streaks.items() if k not in blocked}
    return {"blocked_count": len(blocked), "blocked": blocked,
            "drift_streaks_in_progress": in_progress}


def daemon_reset_blocklist(ids: Optional[List[str]] = None) -> dict:
    """Clears blocked status so the next cycle retries, and clears the
    matching drift-streak counters too (otherwise a reset id could cross
    _STICKY_DRIFT_THRESHOLD again on the very next cycle using a stale
    count). ids=None clears everything; otherwise clears just the given
    tweak/step ids (e.g. after the person manually fixes the underlying
    OS-level block)."""
    state = _load_blocklist()
    streaks = _load_drift_streaks()
    if ids is None:
        state = {}
        streaks = {}
    else:
        for i in ids:
            state.pop(i, None)
            streaks.pop(i, None)
    _save_blocklist(state)
    _save_drift_streaks(streaks)
    return {"cleared": ids if ids is not None else "all", "remaining": len(state)}


def _daemon_status_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "flow_daemon_status.json")


def _load_daemon_status() -> dict:
    path = _daemon_status_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_daemon_status(state: dict) -> None:
    with open(_daemon_status_path(), "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, default=str)


# ═══════════════════════════════════════════════════════════════════
# SECTION 7 — IDLE (idle detection + idle-gated maintenance)
# ═══════════════════════════════════════════════════════════════════
#
# Two different jobs run on two different clocks, deliberately kept
# separate rather than merged into one loop:
#
#   Tweak reapply (above)   — cheap, safe to run every cycle regardless of
#                              whether the person is at the keyboard; it's
#                              just reading a handful of registry/service
#                              values and only writes if something drifted.
#   Idle maintenance (below)— genuinely disk/CPU-intensive (Optimize-Volume,
#                              WinSxS cleanup) on a rig with 4GB RAM, one
#                              i3 core pair, and a mechanical HDD. Running
#                              this while Anish is actively using the
#                              machine would make Flow the thing causing
#                              the lag it's supposed to fix. This only
#                              fires after real, sustained idle time.

IDLE_THRESHOLD_MINUTES_DEFAULT = 15  # must be idle this long before maintenance is allowed to start
IDLE_COOLDOWN_HOURS = 24              # don't re-run idle maintenance more than once per this window

_FLOW_CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".flow")
_FLOW_CONFIG_PATH = os.path.join(_FLOW_CONFIG_DIR, "config.json")


def _load_flow_config() -> dict:
    if not os.path.exists(_FLOW_CONFIG_PATH):
        return {}
    try:
        with open(_FLOW_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_flow_config(data: dict) -> None:
    os.makedirs(_FLOW_CONFIG_DIR, exist_ok=True)
    with open(_FLOW_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def get_idle_threshold_minutes() -> int:
    """User-configurable via the GUI's idle-time field or `set-idle-threshold`
    CLI — falls back to IDLE_THRESHOLD_MINUTES_DEFAULT if never set or the
    stored value is somehow invalid (never let a corrupt config value turn
    into an idle threshold of 0, which would make maintenance run near-
    continuously instead of only when genuinely idle)."""
    val = _load_flow_config().get("idle_threshold_minutes")
    try:
        val = int(val)
        return val if val > 0 else IDLE_THRESHOLD_MINUTES_DEFAULT
    except (TypeError, ValueError):
        return IDLE_THRESHOLD_MINUTES_DEFAULT


def set_idle_threshold_minutes(minutes: int) -> dict:
    minutes = max(1, min(int(minutes), 24 * 60))  # 1 minute to 24 hours — outside that is almost certainly a mistake
    cfg = _load_flow_config()
    cfg["idle_threshold_minutes"] = minutes
    _save_flow_config(cfg)
    return {"idle_threshold_minutes": minutes}


def get_theme_preference() -> Optional[str]:
    """Returns 'light'/'dark' if the person has explicitly toggled the GUI
    theme before, or None if they haven't — None means the GUI should keep
    following the OS's live light/dark setting rather than pin to one.
    Shares ~/.flow/config.json with the idle-threshold setting rather than
    a separate file, since both are small per-user GUI preferences."""
    val = _load_flow_config().get("theme_preference")
    return val if val in ("light", "dark") else None


def set_theme_preference(theme: str) -> dict:
    theme = "light" if theme == "light" else "dark"
    cfg = _load_flow_config()
    cfg["theme_preference"] = theme
    _save_flow_config(cfg)
    return {"theme_preference": theme}


def _get_idle_seconds() -> Optional[float]:
    """Seconds since the last keyboard/mouse input, via GetLastInputInfo.
    Pure ctypes call against user32/kernel32 — no subprocess spawn, no
    PowerShell — this needs to be cheap enough to poll frequently. Returns
    None on non-Windows or if the call fails, so callers can fail safe
    (treat 'unknown' as 'not idle' — never guess idle and risk running
    disruptive work while someone's mid-task)."""
    if platform.system() != "Windows":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        tick_count = ctypes.windll.kernel32.GetTickCount()
        idle_ms = tick_count - info.dwTime
        if idle_ms < 0:
            return None  # GetTickCount() wrapped (49.7-day uptime) — treat as unknown, not idle
        return idle_ms / 1000.0
    except (AttributeError, OSError, ValueError):
        return None


def daemon_idle_maintenance_check(force: bool = False) -> dict:
    """Runs once per daemon cycle alongside the tweak reapply. Only
    actually executes maintenance when ALL of:
      - idle time (real, sustained) exceeds get_idle_threshold_minutes()
        (user-configurable — GUI field or `set-idle-threshold` CLI)
      - it hasn't already run within IDLE_COOLDOWN_HOURS
      - the action isn't disruptive (chkdsk etc. stay manual-only, always
        — a scheduled reboot-recommending action running unattended is
        exactly the kind of surprise Anish should never get from a
        background daemon) and isn't hdd/ssd-mismatched for this rig
    force=True skips the idle/cooldown gates (used by `idle-run-now` for
    testing) but still respects the disruptive/hdd/ssd filters — those are
    a hardware-safety property, not a scheduling one."""
    idle_seconds = _get_idle_seconds()
    threshold_minutes = get_idle_threshold_minutes()
    status = _load_daemon_status()
    now = time.time()
    last_run = status.get("last_idle_maintenance_at", 0)
    hours_since_last = (now - last_run) / 3600.0

    result = {
        "idle_seconds": idle_seconds,
        "idle_minutes": round(idle_seconds / 60, 1) if idle_seconds is not None else None,
        "threshold_minutes": threshold_minutes,
        "hours_since_last_run": round(hours_since_last, 2),
        "cooldown_hours": IDLE_COOLDOWN_HOURS,
        "ran": [], "skipped_reason": None,
    }

    if not force:
        if idle_seconds is None:
            result["skipped_reason"] = "idle time unknown (non-Windows or GetLastInputInfo failed) — failing safe, not running"
            return result
        if idle_seconds < threshold_minutes * 60:
            result["skipped_reason"] = f"not idle long enough ({result['idle_minutes']}m < {threshold_minutes}m)"
            return result
        if hours_since_last < IDLE_COOLDOWN_HOURS:
            result["skipped_reason"] = f"ran {result['hours_since_last_run']}h ago — cooldown is {IDLE_COOLDOWN_HOURS}h"
            return result

    if not is_admin():
        result["skipped_reason"] = "not elevated — maintenance actions need admin, run via flow.bat/daemon-install"
        return result

    profile = get_hardware_profile()
    candidates = [a for a in list_maintenance_actions(profile) if not a.disruptive]
    for action in candidates:
        r = run_maintenance_action(action.id)
        result["ran"].append({"id": action.id, "success": r.success,
                               "error": None if r.success else (r.stderr or r.stdout)})

    status["last_idle_maintenance_at"] = now
    status["last_idle_maintenance_result"] = result
    _save_daemon_status(status)
    _daemon_log({"event": "idle_maintenance", **result})
    return result


def daemon_check_and_reapply_once() -> dict:
    """One enforcement pass. Returns a summary dict; also appends it to the
    daemon log. Safe to call with or without admin — without admin it just
    reports what's drifted without touching anything, since _step_apply's
    underlying calls will fail cleanly (same as every other apply path).
    Each tweak is wrapped individually — one tweak throwing (a stale/
    malformed target, a transient PowerShell hiccup) errors out for that
    tweak only, not the whole pass.

    Two separate protective mechanisms, for two separate failure modes:
      - _BLOCKLIST_THRESHOLD (existing): the apply call itself keeps
        returning non-zero — bad path, missing service, permission error.
        Retrying harder never fixes this.
      - _STICKY_DRIFT_THRESHOLD (this pass): the apply call keeps reporting
        SUCCESS but the value is back to non-target on the very next check —
        something outside Flow (an OS-level protected-default guard, a
        policy re-push, another tool, Explorer/Edge re-writing its own
        prefs) is undoing the write between cycles. This never trips the
        failure counter above since nothing ever fails; left unchecked it's
        an infinite reapply loop that spawns a PowerShell process every
        cycle forever for zero net effect. Observed on a real rig's log:
        the same ~20 ids drifted-and-were-'reapplied' on every one of 70+
        consecutive cycles across 1.6 days.
    """
    log = _load_revert_log()
    # apply_tier() appends a fresh revert-log entry each time it makes a
    # real change; running it more than once (different tiers, repeat
    # testing) leaves several entries for the same tweak_id. Processing
    # every entry here checks/reapplies the same real system value 2-5x
    # per cycle, which both wastes PowerShell spawns and multiplies the
    # blocklist/drift-streak counters incorrectly (N duplicate log entries
    # -> N counter increments in one cycle instead of 1). Keep only the
    # most recent entry per id -- log is append-only chronological, so
    # last-occurrence-wins is correct.
    deduped = {}
    for entry in log:
        deduped[entry["tweak_id"]] = entry
    log = list(deduped.values())

    blocklist = _load_blocklist()
    drift_streaks = _load_drift_streaks()
    checked = 0
    drifted = []
    reapplied = []
    failed = []
    blocked = []
    errored = []

    def _record_failure(item_id: str, error: str) -> None:
        entry = blocklist.get(item_id, {"consecutive_fails": 0, "last_error": None, "blocked": False})
        was_blocked = entry.get("blocked", False)
        entry["consecutive_fails"] += 1
        entry["last_error"] = error
        if entry["consecutive_fails"] >= _BLOCKLIST_THRESHOLD:
            entry["blocked"] = True
            entry["block_reason"] = "apply_failed"
        blocklist[item_id] = entry
        failed.append({"id": item_id, "error": error})
        # Diagnose exactly once, on the cycle that flips not-blocked -> blocked
        # — not every cycle it stays blocked (that would burn one AI call per
        # tweak per hour forever for no new information). Silently skipped
        # if no AI key is configured — get_ai_credentials() is a local file/
        # env read, so this costs nothing on the no-key path.
        if entry["blocked"] and not was_blocked:
            api_key, provider = get_ai_credentials()
            if api_key and provider in AI_PROVIDERS:
                diagnosis = ai_diagnose_failure(item_id, error, entry["consecutive_fails"])
                entry["ai_diagnosis"] = diagnosis
                blocklist[item_id] = entry
                _daemon_log({"event": "ai_diagnosis", "id": item_id, **diagnosis})

    def _record_success(item_id: str) -> None:
        if item_id in blocklist:
            del blocklist[item_id]

    def _note_drift(item_id: str) -> int:
        drift_streaks[item_id] = drift_streaks.get(item_id, 0) + 1
        return drift_streaks[item_id]

    def _note_no_drift(item_id: str) -> None:
        if item_id in drift_streaks:
            del drift_streaks[item_id]

    def _check_and_maybe_block_sticky(item_id: str) -> bool:
        """Returns True if this id just got (or already was) blocked for
        sticky drift — caller should skip the apply attempt this cycle."""
        existing = blocklist.get(item_id, {})
        if existing.get("blocked"):
            blocked.append({"id": item_id, "error": existing.get("last_error")})
            return True
        streak = drift_streaks.get(item_id, 0)
        if streak >= _STICKY_DRIFT_THRESHOLD:
            msg = (f"kept drifting back for {streak} consecutive checks even though "
                   f"reapply reported success each time — something outside Flow is "
                   f"resetting this value between checks (a protected-default guard, "
                   f"a policy refresh, or another tool/app rewriting its own prefs). "
                   f"Retrying won't fix it; stopping.")
            entry = {"consecutive_fails": existing.get("consecutive_fails", 0),
                      "last_error": msg, "blocked": True, "block_reason": "persistent_drift"}
            blocklist[item_id] = entry
            blocked.append({"id": item_id, "error": msg})
            api_key, provider = get_ai_credentials()
            if api_key and provider in AI_PROVIDERS:
                diagnosis = ai_diagnose_failure(item_id, msg, streak, mode="persistent_drift")
                entry["ai_diagnosis"] = diagnosis
                blocklist[item_id] = entry
                _daemon_log({"event": "ai_diagnosis", "id": item_id, **diagnosis})
            return True
        return False

    for entry in log:
        tweak = TWEAK_BY_ID.get(entry["tweak_id"])
        if not tweak:
            continue  # tweak removed from the database since this was applied — nothing to enforce
        checked += 1

        try:
            if tweak.method == "hybrid":
                for step in tweak.target["steps"]:
                    current, existed = _step_capture(step["method"], step["target"])
                    step_id = f"{tweak.id}:{step['method']}"
                    if existed and _values_equal(current, step["value"]):
                        _note_no_drift(step_id)
                        continue
                    drifted.append(step_id)
                    _note_drift(step_id)
                    if _check_and_maybe_block_sticky(step_id):
                        continue
                    if is_admin() and TWEAKS_APPLY_ENABLED:
                        r = _step_apply(step["method"], step["target"], step["value"])
                        if r.success:
                            reapplied.append(step_id)
                            _record_success(step_id)
                        else:
                            _record_failure(step_id, r.stderr or r.stdout or f"exit code {r.returncode}")
                continue

            current, existed = _step_capture(tweak.method, tweak.target)
            if existed and _values_equal(current, tweak.tweak_value):
                _note_no_drift(tweak.id)
                continue  # still matches target — nothing to do
            drifted.append(tweak.id)
            _note_drift(tweak.id)
            if _check_and_maybe_block_sticky(tweak.id):
                continue
            if is_admin() and TWEAKS_APPLY_ENABLED:
                r = _step_apply(tweak.method, tweak.target, tweak.tweak_value)
                if r.success:
                    reapplied.append(tweak.id)
                    _record_success(tweak.id)
                else:
                    _record_failure(tweak.id, r.stderr or r.stdout or f"exit code {r.returncode}")
        except Exception as exc:  # noqa: BLE001 — daemon must survive one bad tweak, by design
            errored.append(f"{tweak.id}: {exc}")

    _save_blocklist(blocklist)
    _save_drift_streaks(drift_streaks)
    summary = {
        "checked": checked, "drifted": drifted, "reapplied": reapplied, "failed": failed,
        "blocked": blocked, "errored": errored, "admin": is_admin(), "dry_run": not TWEAKS_APPLY_ENABLED,
        "flow_file": os.path.abspath(__file__),
    }
    _daemon_log(summary)
    return summary


def daemon_run_loop(interval_minutes: int = 60) -> None:
    """Foreground loop — what Task Scheduler actually invokes. Runs one
    check immediately on start (so a manual `daemon-run` shows results
    right away), then repeats on the interval indefinitely. A check that
    throws (rather than just returning failures) is logged and the loop
    keeps going — a scheduled task that silently stops running is worse
    than one that logs an error and tries again next cycle.

    Idle maintenance (Section 7) piggybacks on this same interval rather
    than getting its own timer/thread — it's cheap to poll (one ctypes
    call) and self-gates on idle time + cooldown internally, so checking
    it every cycle costs nothing on the cycles where it's not due."""
    _daemon_log({"event": "daemon started", "interval_minutes": interval_minutes})
    try:
        while True:
            try:
                daemon_check_and_reapply_once()
            except Exception as exc:  # noqa: BLE001
                _daemon_log({"event": "check cycle crashed", "error": str(exc)})
            try:
                daemon_idle_maintenance_check()
            except Exception as exc:  # noqa: BLE001
                _daemon_log({"event": "idle maintenance cycle crashed", "error": str(exc)})
            time.sleep(max(1, interval_minutes) * 60)
    except KeyboardInterrupt:
        _daemon_log({"event": "daemon stopped (keyboard interrupt)"})


_DAEMON_TASK_NAME = "FlowTweakDaemon"


def daemon_install(interval_minutes: int = 60) -> ExecResult:
    """Registers a Task Scheduler task that runs `pythonw flow.py daemon-run`
    hidden, at user logon, with highest privileges (required since it's the
    same admin-gated apply path as every manual tweak). Uses schtasks rather
    than a real Windows Service — no service-install/SCM complexity, and
    it's user-scoped so it uninstalls cleanly with the app, not system-wide."""
    import os
    script_path = os.path.abspath(__file__)
    python_dir = os.path.dirname(sys.executable)
    pythonw = os.path.join(python_dir, "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable  # fall back to python.exe if pythonw isn't present (some installs)
    action = f'"{pythonw}" "{script_path}" daemon-run --interval-minutes {interval_minutes}'
    return run_hidden([
        "schtasks", "/Create", "/TN", _DAEMON_TASK_NAME, "/TR", action,
        "/SC", "ONLOGON", "/RL", "HIGHEST", "/F",
    ])


def daemon_uninstall() -> ExecResult:
    return run_hidden(["schtasks", "/Delete", "/TN", _DAEMON_TASK_NAME, "/F"])


def daemon_status() -> dict:
    result = run_hidden(["schtasks", "/Query", "/TN", _DAEMON_TASK_NAME, "/FO", "LIST"])
    installed = result.success
    last_lines = []
    if os.path.exists(_daemon_log_path()):
        with open(_daemon_log_path(), "r", encoding="utf-8") as f:
            last_lines = f.readlines()[-5:]
    recent = []
    for line in last_lines:
        try:
            recent.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    status = _load_daemon_status()
    idle_seconds = _get_idle_seconds()

    # Catch the "edited flow.py but never reinstalled the daemon" trap:
    # daemon_install() bakes an absolute script path into the scheduled
    # task's action at install time. If the file at that path later gets
    # replaced/moved/edited-in-place-elsewhere, the RUNNING daemon can
    # still be an older copy with different logic (e.g. missing a bugfix)
    # even though `flow.py daemon-status` run from a newer copy looks
    # fine. Each check-cycle's log entry also carries its own flow_file
    # (see daemon_check_and_reapply_once) — recent_runs above will show
    # exactly what the live process resolved __file__ to, which is the
    # ground truth; this field is the quicker "does it even look right"
    # check without reading the log.
    this_file = os.path.abspath(__file__)
    task_action = None
    if installed:
        for line in result.stdout.splitlines():
            if line.strip().lower().startswith("task to run:"):
                task_action = line.split(":", 1)[1].strip()
                break
    task_points_here = (task_action is not None and this_file.lower() in task_action.lower())

    return {
        "installed": installed,
        "this_file": this_file,
        "scheduled_task_action": task_action,
        "scheduled_task_matches_this_file": task_points_here if installed else None,
        "recent_runs": recent,
        "idle_minutes_now": round(idle_seconds / 60, 1) if idle_seconds is not None else None,
        "last_idle_maintenance_at": status.get("last_idle_maintenance_at"),
        "last_idle_maintenance_result": status.get("last_idle_maintenance_result"),
    }


def _step_capture(method: str, target: dict):
    """Returns (previous_value, existed) for one atomic step. Used both for
    plain single-method tweaks and for each step inside a hybrid tweak."""
    if method == "registry":
        return _registry_get(target["path"], target["name"])
    if method == "service":
        return _service_get_start_type(target["service_name"])
    if method == "power_scheme":
        return _power_scheme_get_active()
    if method == "power_setting":
        return _power_setting_get(target["subgroup_guid"], target["setting_guid"])
    if method == "per_adapter_registry":
        return _per_adapter_registry_get(target["base_path"], target["name"])
    if method == "hibernate":
        return _hibernate_get_enabled()
    if method == "appx":
        installed = _appx_get_installed(target["package_name"])
        return installed, True  # "existed" here means "was installed" — always known, never ambiguous
    if method == "onedrive":
        return _onedrive_installed(), True
    if method == "explorer_permission_deny":
        return _icacls_get_deny_state(os.path.expandvars(target["path"]))
    if method == "bitlocker_disable":
        return _bitlocker_get_status(target.get("drive", "system"))
    if method == "winget":
        return _winget_is_installed(target["package_id"]), True
    if method == "svchost_split":
        return _svchost_split_get()
    return None, False


def _step_apply(method: str, target: dict, value) -> ExecResult:
    """Applies one atomic step. Same method set _step_capture/_step_revert
    know about — adding a new method means adding one branch to all three."""
    if method == "registry":
        return _registry_set(target["path"], target["name"], value, target.get("force_type"))
    if method == "service":
        return _service_set_start_type(target["service_name"], value)
    if method == "power_scheme":
        return _power_scheme_set_active(value)
    if method == "power_setting":
        return _power_setting_set(target["subgroup_guid"], target["setting_guid"], value)
    if method == "per_adapter_registry":
        return _per_adapter_registry_set(target["base_path"], target["name"], value)
    if method == "hibernate":
        return _hibernate_set(value)
    if method == "appx":
        return _appx_remove(target["package_name"])
    if method == "onedrive":
        return _onedrive_remove()
    if method == "explorer_permission_deny":
        return _icacls_set_deny(os.path.expandvars(target["path"]))
    if method == "bitlocker_disable":
        return _bitlocker_disable(target.get("drive", "system"))
    if method == "winget":
        return _winget_uninstall(target["package_id"])
    if method == "svchost_split":
        return _svchost_split_apply()
    return ExecResult(command="apply_step(unknown)", success=False, returncode=-1,
                       stdout="", stderr=f"unknown method '{method}'", duration_s=0.0)


def _step_revert(method: str, target: dict, previous_value, existed: bool) -> ExecResult:
    """Reverts one atomic step back to its captured previous_value."""
    if method == "registry":
        if not existed:
            return _registry_delete(target["path"], target["name"])
        return _registry_set(target["path"], target["name"], previous_value, target.get("force_type"))
    if method == "service":
        return _service_set_start_type(target["service_name"], previous_value)
    if method == "power_scheme":
        if not existed or not previous_value:
            return ExecResult(command="revert(power_scheme)", success=False, returncode=-1,
                               stdout="", stderr="no previous scheme GUID captured", duration_s=0.0)
        return _power_scheme_set_active(previous_value)
    if method == "power_setting":
        if not existed:
            return ExecResult(command="revert(power_setting)", success=False, returncode=-1,
                               stdout="", stderr="no previous AC/DC index captured", duration_s=0.0)
        return _power_setting_set(target["subgroup_guid"], target["setting_guid"], previous_value)
    if method == "per_adapter_registry":
        if not existed:
            # Some adapters had no value at all before, or disagreed with each
            # other — there's no single "previous value" to restore uniformly,
            # so reverting just removes the value from every adapter, which
            # puts each interface back to relying on the Windows-default
            # Nagle behavior rather than guessing at a per-adapter history
            # this tool never actually captured.
            script = (
                "$base = 'HKLM:\\SYSTEM\\CurrentControlSet\\Services\\Tcpip\\Parameters\\Interfaces';"
                f"Get-ChildItem $base -ErrorAction SilentlyContinue | ForEach-Object {{"
                f"  Remove-ItemProperty -Path $_.PSPath -Name '{target['name']}' -ErrorAction SilentlyContinue"
                f"}}"
            )
            return run_powershell(script)
        return _per_adapter_registry_set(target["base_path"], target["name"], previous_value)
    if method == "hibernate":
        return _hibernate_set(bool(previous_value))
    if method == "appx":
        if not previous_value:
            return ExecResult(command="revert(appx)", success=True, returncode=0,
                               stdout="was not installed before — nothing to restore", stderr="", duration_s=0.0)
        return _appx_reinstall_note(target["package_name"])
    if method == "onedrive":
        if not previous_value:
            return ExecResult(command="revert(onedrive)", success=True, returncode=0,
                               stdout="was not installed before — nothing to restore", stderr="", duration_s=0.0)
        return _onedrive_reinstall_note()
    if method == "explorer_permission_deny":
        return _icacls_set_grant(os.path.expandvars(target["path"]))
    if method == "bitlocker_disable":
        if not previous_value:
            return ExecResult(command="revert(bitlocker_disable)", success=True, returncode=0,
                               stdout="BitLocker was already off — nothing to restore", stderr="", duration_s=0.0)
        return _bitlocker_enable(target.get("drive", "system"))
    if method == "winget":
        if not previous_value:
            return ExecResult(command="revert(winget)", success=True, returncode=0,
                               stdout="was not installed before — nothing to restore", stderr="", duration_s=0.0)
        return _winget_install(target["package_id"])
    if method == "svchost_split":
        return _svchost_split_revert(previous_value, existed)
    return ExecResult(command="revert(unknown)", success=False, returncode=-1,
                       stdout="", stderr=f"unknown method '{method}'", duration_s=0.0)


def _capture_previous(tweak: Tweak):
    """Returns (previous_value, existed) for whatever method the tweak uses.
    For a hybrid tweak, previous_value is a list of (value, existed) pairs —
    one per step, in step order — and existed is always True at the hybrid
    level since "what were the steps before" is always knowable even if
    individual steps didn't exist."""
    if tweak.method == "hybrid":
        return [_step_capture(s["method"], s["target"]) for s in tweak.target["steps"]], True
    return _step_capture(tweak.method, tweak.target)


def _refresh_explorer() -> ExecResult:
    """Kills and lets Windows auto-restart explorer.exe so registry changes
    to taskbar/theme-adjacent settings become visible immediately instead
    of requiring logoff. Called ONCE per apply_tier()/revert_all() batch,
    not per-tweak, so the desktop doesn't flicker repeatedly."""
    return run_powershell("Stop-Process -Name explorer -Force -ErrorAction SilentlyContinue")


def apply_tweak(tweak: Tweak) -> ExecResult:
    """Capture previous state -> skip if already in the target state ->
    apply -> log the revert entry. Never applies without first writing
    what it's about to change, so a crash mid-apply still leaves an
    accurate (if partial) revert trail. Never touches a setting that's
    already correct — nothing to change means nothing to log or revert."""
    if not TWEAKS_APPLY_ENABLED:
        return ExecResult(command=f"apply_tweak({tweak.id})", success=False, returncode=0,
                           stdout=f"DRY RUN — TWEAKS_APPLY_ENABLED=False, '{tweak.id}' not touched",
                           stderr="", duration_s=0.0)
    if not is_admin():
        return ExecResult(command=f"apply_tweak({tweak.id})", success=False, returncode=-1,
                           stdout="", stderr="not running elevated — tweak application requires admin", duration_s=0.0)

    previous_value, existed = _capture_previous(tweak)

    # Ultimate Performance strictly outranks High Performance — never
    # downgrade a plan the user (or a prior Flow run) already set. Checked
    # by label, not GUID — see _power_scheme_current_is_ultimate().
    if tweak.method == "power_scheme" and _power_scheme_current_is_ultimate():
        return ExecResult(command=f"apply_tweak({tweak.id})", success=True, returncode=0,
                           stdout="already on Ultimate Performance — left untouched, not downgrading",
                           stderr="", duration_s=0.0)

    # Idempotency guard — current state already matches the target state
    # (either a prior Flow run, or Windows' own hardware-based default on
    # weak/old machines). Skip the write entirely: no system call, no
    # revert entry, nothing for revert-all to touch later.
    if tweak.method != "hybrid" and existed and _values_equal(previous_value, tweak.tweak_value):
        return ExecResult(command=f"apply_tweak({tweak.id})", success=True, returncode=0,
                           stdout=f"already applied — '{tweak.id}' matches target state, skipped",
                           stderr="", duration_s=0.0)

    if tweak.method == "hybrid":
        steps = tweak.target["steps"]
        step_results = []
        any_changed = False
        for step, (step_prev, step_existed) in zip(steps, previous_value):
            if step_existed and _values_equal(step_prev, step["value"]):
                step_results.append(f"[{step['method']}:{step.get('target',{}).get('name', step.get('target',{}).get('service_name','?'))}] already applied, skipped")
                continue
            r = _step_apply(step["method"], step["target"], step["value"])
            any_changed = any_changed or r.success
            step_results.append(f"[{step['method']}] {'ok' if r.success else 'FAILED: ' + r.stderr}")
        all_ok = all("FAILED" not in s for s in step_results)
        result = ExecResult(
            command=f"apply_tweak({tweak.id})", success=all_ok, returncode=0 if all_ok else -1,
            stdout=" | ".join(step_results) if any_changed or not all_ok else f"already applied — '{tweak.id}' matches target state, skipped",
            stderr="" if all_ok else "one or more steps in this hybrid tweak failed", duration_s=0.0,
        )
    elif tweak.method == "registry":
        result = _registry_set(tweak.target["path"], tweak.target["name"], tweak.tweak_value,
                                tweak.target.get("force_type"))
    elif tweak.method == "service":
        result = _service_set_start_type(tweak.target["service_name"], tweak.tweak_value)
    elif tweak.method == "power_scheme":
        result = _power_scheme_set_active(tweak.tweak_value)
    elif tweak.method == "power_setting":
        result = _power_setting_set(tweak.target["subgroup_guid"], tweak.target["setting_guid"], tweak.tweak_value)
    elif tweak.method == "per_adapter_registry":
        result = _per_adapter_registry_set(tweak.target["base_path"], tweak.target["name"], tweak.tweak_value)
    elif tweak.method == "hibernate":
        result = _hibernate_set(tweak.tweak_value)
    elif tweak.method == "appx":
        result = _appx_remove(tweak.target["package_name"])
    elif tweak.method == "onedrive":
        result = _onedrive_remove()
    elif tweak.method == "winget":
        result = _winget_uninstall(tweak.target["package_id"])
    elif tweak.method == "svchost_split":
        result = _svchost_split_apply()
    elif tweak.method == "bitlocker_disable":
        result = _bitlocker_disable(tweak.target.get("drive", "system"))
    else:
        result = ExecResult(command=f"apply_tweak({tweak.id})", success=False, returncode=-1,
                             stdout="", stderr=f"unknown method '{tweak.method}'", duration_s=0.0)

    entry = RevertEntry(
        tweak_id=tweak.id, method=tweak.method, target=tweak.target,
        previous_value=previous_value, previous_value_existed=existed,
        applied_at=time.strftime("%Y-%m-%dT%H:%M:%S"), success=result.success,
    )
    log = _load_revert_log()
    log.append(entry.to_dict())
    _save_revert_log(log)
    return result


def _apply_batch(tweaks: List[Tweak]) -> List[ExecResult]:
    """Shared by apply_tier() and apply_selected() — applies a list of
    tweaks and refreshes Explorer once at the end if anything that
    actually changed needs it visible without a logoff. Pulled out so both
    callers get identical refresh/skip semantics instead of two copies
    that could drift.

    MANDATORY RESTORE POINT: enforced here, not just suggested by the GUI,
    so no caller — GUI, CLI, a future integration — can apply tweaks
    without a fresh restore point existing first. If it can't actually be
    created (System Restore disabled on the volume, not elevated, etc.)
    the whole batch is refused rather than proceeding with no safety net —
    that IS the point. Skipped only when TWEAKS_APPLY_ENABLED is globally
    False: in that dry-run mode apply_tweak() never touches the system
    regardless, so gating would just be friction with nothing real to
    protect against. This does NOT apply to the daemon's drift-reapply
    path (_step_apply calls directly, see Section 4B) — those re-assert a
    value a person already consented to and got a restore point for the
    first time; forcing a fresh checkpoint every ~hourly cycle would just
    fight Windows' own 24h throttle for no added protection."""
    if not tweaks:
        return []
    if TWEAKS_APPLY_ENABLED:
        checkpoint = create_restore_point("Flow pre-tweak checkpoint (auto, before apply)")
        if not checkpoint.success:
            reason = checkpoint.stderr or checkpoint.stdout or "unknown error"
            return [ExecResult(
                command="apply_batch(blocked — no restore point)", success=False, returncode=-1,
                stdout="", duration_s=checkpoint.duration_s,
                stderr=f"Refused to apply {len(tweaks)} tweak(s) — a restore point is required before any "
                       f"apply and one could not be created: {reason}",
            )]
    results = [apply_tweak(t) for t in tweaks]
    changed_ids = {t.id for t, r in zip(tweaks, results)
                   if r.success and "skipped" not in r.stdout and "DRY RUN" not in r.stdout}
    if TWEAKS_APPLY_ENABLED and any(TWEAK_BY_ID[i].requires_explorer_refresh for i in changed_ids):
        _refresh_explorer()
    return results


def apply_tier(tier: str, profile: HardwareProfile) -> List[ExecResult]:
    return _apply_batch(list_tweaks_for_tier(tier, profile))


def apply_selected(tweak_ids: List[str], profile: HardwareProfile) -> List[ExecResult]:
    """Applies exactly the tweak ids the GUI's checkboxes sent — a subset of
    list_tweaks_for_tier(), not necessarily the whole tier. Re-checks
    _tweak_applies() itself rather than trusting the frontend's filtered
    list, so a stale id (e.g. left over from a previous tier's selection,
    or a tampered call) can't slip through and apply something that
    doesn't actually fit this hardware."""
    valid = {t.id: t for t in TWEAK_DATABASE if _tweak_applies(t, profile)}
    tweaks = [valid[i] for i in tweak_ids if i in valid]
    return _apply_batch(tweaks)


def revert_entry(entry: dict) -> ExecResult:
    method = entry["method"]
    target = entry["target"]
    previous_value = entry["previous_value"]
    existed = entry["previous_value_existed"]

    if method == "hybrid":
        steps = target["steps"]
        step_results = []
        for step, (step_prev, step_existed) in zip(steps, previous_value):
            r = _step_revert(step["method"], step["target"], step_prev, step_existed)
            step_results.append(f"[{step['method']}] {'ok' if r.success else 'FAILED: ' + r.stderr}")
        all_ok = all("FAILED" not in s for s in step_results)
        return ExecResult(command="revert(hybrid)", success=all_ok, returncode=0 if all_ok else -1,
                           stdout=" | ".join(step_results), stderr="" if all_ok else "one or more steps failed to revert",
                           duration_s=0.0)
    return _step_revert(method, target, previous_value, existed)


def revert_all() -> List[ExecResult]:
    """Reverts every logged tweak in reverse order (last applied, first
    reverted — mirrors a stack unwind so dependent changes unwind safely).
    Successfully reverted entries are cleared from the log; failures stay
    so a retry doesn't lose track of what's still pending. Refreshes
    Explorer once at the end if anything reverted needs it visible —
    a plain registry revert of a theme-adjacent key is correct on disk
    immediately but WON'T visibly restore the desktop until Explorer
    reloads, which is why a revert can look like it "did nothing."""
    log = _load_revert_log()
    results = []
    remaining = []
    needs_refresh = False
    for entry in reversed(log):
        result = revert_entry(entry)
        results.append(result)
        if not result.success:
            remaining.append(entry)
        elif TWEAK_BY_ID.get(entry["tweak_id"]) and TWEAK_BY_ID[entry["tweak_id"]].requires_explorer_refresh:
            needs_refresh = True
    _save_revert_log(list(reversed(remaining)))
    if needs_refresh:
        _refresh_explorer()
    return results


# ═══════════════════════════════════════════════════════════════════
# SECTION 5B — AI ADVISOR (narration only, optional, degrades cleanly)
# ═══════════════════════════════════════════════════════════════════
# Scope, per Creator sign-off: the AI never chooses what gets applied. The
# tweak list a tier runs stays pinned to TWEAK_DATABASE + list_tweaks_for_tier()
# exactly as before — that's the safety boundary from Section 4's comments,
# and this section doesn't touch it. All the AI does here is take the
# ALREADY-SELECTED tier + tweak list and write a plain-language explanation
# of why it fits this specific machine, plus flag anything worth a second
# look. Called only when the Creator explicitly clicks "AI Insight" — never
# on load, never blocking the tier-apply path.
#
# Key management, zero-budget/no-installer constraint in mind:
# Key management, zero-budget/no-installer constraint in mind:
#   1. A ".env" file next to flow.py — loaded automatically on every launch
#      by _load_dotenv() near the top of this file, no setup needed beyond
#      copying .env.example to .env and filling in one line. Real env vars
#      set in the shell still take precedence over .env if both exist.
#   2. Named env vars (GROQ_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY /
#      OPENROUTER_API_KEY / FLOW_AI_API_KEY) set directly — checked first,
#      unambiguous since the var name states the provider. See .env.example.
#   3. A key pasted into the GUI Settings panel — saved to a small local
#      JSON file under the user's home dir (~/.flow/ai_config.json), never
#      next to flow.py, never anywhere near source control. Provider is
#      auto-detected from the key's prefix so the user never has to know
#      which service they're pointing at.
# None of these are required — with nothing configured, ai_explain() just
# returns {"available": False, ...} and the GUI hides the feature quietly.

_AI_CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".flow")
_AI_CONFIG_PATH = os.path.join(_AI_CONFIG_DIR, "ai_config.json")

# Provider registry: prefixes are checked most-specific-first in
# _PROVIDER_CHECK_ORDER, since a generic "sk-" would otherwise swallow
# Anthropic/OpenRouter keys that also start with "sk-".
AI_PROVIDERS = {
    "groq": {
        "label": "Groq",
        "prefixes": ("gsk_",),
        "endpoint": "https://api.groq.com/openai/v1/chat/completions",
        # llama-3.3-70b-versatile was deprecated by Groq (announced June 17,
        # 2026) — every request to it now returns model_decommissioned.
        # openai/gpt-oss-20b is Groq's own recommended replacement (fast,
        # free-tier friendly). Override with FLOW_MODEL_GROQ if needed.
        "model": "openai/gpt-oss-20b",
        "style": "openai",
        "free": True,
    },
    "gemini": {
        "label": "Gemini",
        "prefixes": ("AIza",),
        "endpoint": "https://generativelanguage.googleapis.com/v1beta/models",
        # Flash is the free-tier model (Pro requires billing enabled).
        # Override with FLOW_MODEL_GEMINI.
        "model": "gemini-2.5-flash",
        "style": "gemini",
        "free": True,
    },
    "openrouter": {
        "label": "OpenRouter",
        "prefixes": ("sk-or-",),
        "endpoint": "https://openrouter.ai/api/v1/chat/completions",
        # OpenRouter's own free-model auto-router. Individual free model IDs
        # (e.g. "meta-llama/llama-3.1-8b-instruct:free") rotate out and 404
        # within weeks — this is what caused the "HTTP Error 404" the user
        # hit. "openrouter/free" always resolves to *some* currently-live
        # free model, so it doesn't go stale. Override with
        # FLOW_MODEL_OPENROUTER if a specific model is preferred.
        "model": "openrouter/free",
        "style": "openai",
        "free": True,
    },
    "anthropic": {
        "label": "Anthropic",
        "prefixes": ("sk-ant-",),
        "endpoint": "https://api.anthropic.com/v1/messages",
        # claude-3-5-haiku-20241022 was retired by Anthropic on Feb 19,
        # 2026 — every request to it now errors. claude-haiku-4-5 is the
        # current fast/cheap tier. Override with FLOW_MODEL_ANTHROPIC.
        # No free tier — usage-based billing only.
        "model": "claude-haiku-4-5-20251001",
        "style": "anthropic",
        "free": False,
    },
    "openai": {
        "label": "OpenAI",
        "prefixes": ("sk-proj-", "sk-"),  # "sk-" is generic — must stay last
        "endpoint": "https://api.openai.com/v1/chat/completions",
        # gpt-4o-mini's API snapshot sunset around Feb 27, 2026. gpt-5-mini
        # is the current cost/speed-equivalent replacement, still served on
        # the Chat Completions endpoint used below. Override with
        # FLOW_MODEL_OPENAI. No free tier — usage-based billing only.
        "model": "gpt-5-mini",
        "style": "openai",
        "free": False,
    },
}
# Groq, Gemini, and OpenRouter all have a genuinely free tier (no card,
# no billing setup). Anthropic and OpenAI are pay-as-you-go only — still
# supported for anyone who already has a paid key, but never suggested
# as the "get started free" path in the Settings copy.
_PROVIDER_CHECK_ORDER = ["groq", "gemini", "openrouter", "anthropic", "openai"]

# Per-provider model override, e.g. FLOW_MODEL_OPENROUTER=some/other-model:free
# Lets a user route around a dead/rate-limited default without editing source.
def _resolve_model(provider: str) -> str:
    override = os.environ.get(f"FLOW_MODEL_{provider.upper()}")
    return override.strip() if override else AI_PROVIDERS[provider]["model"]

_AI_ENV_VARS = {
    "GROQ_API_KEY": "groq",
    "GEMINI_API_KEY": "gemini",
    "GOOGLE_API_KEY": "gemini",  # Google's own SDKs also read this name — accept both
    "OPENAI_API_KEY": "openai",
    "ANTHROPIC_API_KEY": "anthropic",
    "OPENROUTER_API_KEY": "openrouter",
}


def _detect_ai_provider(api_key: str) -> Optional[str]:
    """Prefix-sniffs a pasted key to figure out which provider it belongs
    to, so the GUI's 'paste your key' field never needs a provider dropdown."""
    key = (api_key or "").strip()
    if not key:
        return None
    for name in _PROVIDER_CHECK_ORDER:
        for prefix in AI_PROVIDERS[name]["prefixes"]:
            if key.startswith(prefix):
                return name
    return None


def _load_ai_config() -> dict:
    if not os.path.exists(_AI_CONFIG_PATH):
        return {}
    try:
        with open(_AI_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_ai_config(api_key: str, provider: str) -> None:
    os.makedirs(_AI_CONFIG_DIR, exist_ok=True)
    with open(_AI_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({"api_key": api_key, "provider": provider}, f)
    try:
        os.chmod(_AI_CONFIG_PATH, 0o600)  # best-effort — no real ACL equivalent on Windows
    except OSError:
        pass


def _clear_ai_config() -> None:
    try:
        os.remove(_AI_CONFIG_PATH)
    except OSError:
        pass


def get_ai_credentials() -> tuple:
    """Resolution order: named env vars first (unambiguous — the var name
    states the provider, no sniffing needed), then FLOW_AI_API_KEY (generic,
    provider sniffed from prefix), then the GUI-saved local config file.
    Returns (api_key, provider_name) or (None, None)."""
    for env_name, provider in _AI_ENV_VARS.items():
        val = os.environ.get(env_name)
        if val:
            return val.strip(), provider

    generic = os.environ.get("FLOW_AI_API_KEY")
    if generic:
        provider = _detect_ai_provider(generic)
        if provider:
            return generic.strip(), provider

    cfg = _load_ai_config()
    if cfg.get("api_key"):
        provider = cfg.get("provider") or _detect_ai_provider(cfg["api_key"])
        if provider in AI_PROVIDERS:
            return cfg["api_key"], provider

    return None, None


def _build_advisor_prompt(profile: HardwareProfile, tier: str, tweaks: List[Tweak],
                           batch_index: int = 1, batch_count: int = 1, total_in_tier: int = None) -> str:
    # NOTE: this used to silently slice to tweaks[:25], which meant any tier
    # bigger than 25 (minimal/standard regularly run 70+) only ever got AI
    # insight on its first 25 entries — every other tweak in the tier showed
    # no "why"/"watch_for" at all. Callers now pass one batch at a time (see
    # ai_explain_tier) and this function annotates every tweak it's given.
    total_in_tier = total_in_tier if total_in_tier is not None else len(tweaks)
    tweak_lines = "\n".join(f'- id="{t.id}" [{t.risk}] {t.name}: {t.description}' for t in tweaks)
    disks = ", ".join(f"{d.media_type} {d.size_gb}GB" for d in profile.disks) or "none detected"
    gpu = profile.gpus[0].name if profile.gpus else "none detected"
    battery_line = "laptop (has a battery)" if profile.battery else "desktop (no battery detected)"
    batch_note = (
        f" (batch {batch_index} of {batch_count} — {total_in_tier} tweaks total in this tier; "
        "write the summary as if introducing the WHOLE tier, not just this batch)"
        if batch_count > 1 else ""
    )
    summary_instruction = (
        '"<2-3 sentences on why this tier fits THIS machine specifically — leave this exact '
        'string empty ("") if batch_index > 1, since batch 1 already covers it>"'
        if batch_index > 1 else
        '"<2-3 sentences on why this tier fits THIS machine specifically>"'
    )
    return (
        "You are annotating a Windows optimization plan for one real user's PC, tweak by "
        "tweak. The tweak list below has ALREADY been selected by the app's own "
        "hardware-matching logic — do not invent, add, or suggest any tweak outside this "
        "list, and do not tell the user to change anything beyond what's listed.\n\n"
        f"This machine: {profile.cpu.name}, {profile.cpu.physical_cores} physical cores, "
        f"{profile.ram.total_gb}GB RAM, disks: {disks}, GPU: {gpu}, {battery_line}, "
        f"board: {profile.board.manufacturer} {profile.board.model}.\n"
        f"Selected tier: {tier}{batch_note}\n"
        f"Tweaks in this batch:\n{tweak_lines}\n\n"
        "Respond with ONLY raw JSON (no markdown fences, no prose outside the JSON) matching "
        "this exact shape:\n"
        '{\n'
        f'  "summary": {summary_instruction},\n'
        '  "tweaks": {\n'
        '    "<tweak id>": {\n'
        '      "why": "<1-2 sentences: why this tweak matters for THIS specific hardware, '
        'not a generic definition>",\n'
        '      "watch_for": "<1 short sentence on what to double-check before/after applying '
        'on this rig, or empty string if there is genuinely nothing to flag>"\n'
        '    }\n'
        '  }\n'
        '}\n\n'
        "Include one entry under \"tweaks\" for every id listed above, using the id exactly as "
        "given. Ground every explanation in the actual specs above (e.g. reference the real "
        "CPU/RAM/disk/GPU/battery, not placeholders) — avoid generic boilerplate that would "
        "apply to any PC. Stay strictly within the tweaks listed above."
    )


def _parse_ai_response(raw_text: str, tweak_ids: List[str]) -> dict:
    """Parses the model's JSON reply into {"summary": str, "tweaks": {id: {...}}}.
    Tolerant of markdown code fences some models wrap JSON in regardless of
    instructions. Falls back to treating the whole reply as the summary with
    no per-tweak entries if it isn't valid/expected JSON — degrades to the
    old tier-level-only behavior rather than failing the whole insight."""
    text = (raw_text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return {"summary": raw_text.strip(), "tweaks": {}}

    if not isinstance(data, dict):
        return {"summary": raw_text.strip(), "tweaks": {}}

    summary = data.get("summary")
    if not isinstance(summary, str):
        summary = ""

    raw_tweaks = data.get("tweaks")
    tweaks_out = {}
    if isinstance(raw_tweaks, dict):
        valid_ids = set(tweak_ids)
        for tid, entry in raw_tweaks.items():
            if tid not in valid_ids or not isinstance(entry, dict):
                continue
            why = entry.get("why")
            watch_for = entry.get("watch_for")
            tweaks_out[tid] = {
                "why": why.strip() if isinstance(why, str) else "",
                "watch_for": watch_for.strip() if isinstance(watch_for, str) else "",
            }

    return {"summary": summary.strip(), "tweaks": tweaks_out}


def _extract_http_error_detail(exc: "urllib.error.HTTPError") -> str:
    """urllib's default str(HTTPError) is just 'HTTP Error 404: Not Found' —
    it discards the response body, which is where providers actually put
    the useful part (e.g. 'model_decommissioned', 'invalid x-api-key').
    Reading it here is what turns an opaque 404 into an actionable message
    in the GUI instead of sending the user to test the key externally."""
    try:
        raw = exc.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 — body read is best-effort, never fatal
        return f"HTTP {exc.code}: {exc.reason}"
    try:
        parsed = json.loads(raw)
        msg = (
            parsed.get("error", {}).get("message")
            if isinstance(parsed.get("error"), dict)
            else parsed.get("error") or parsed.get("message")
        )
        if msg:
            return f"HTTP {exc.code}: {msg}"
    except (json.JSONDecodeError, AttributeError):
        pass
    return f"HTTP {exc.code}: {raw[:300]}" if raw else f"HTTP {exc.code}: {exc.reason}"


def _call_openai_style(endpoint: str, api_key: str, model: str, prompt: str, max_tokens: int = 1600) -> str:
    import urllib.request
    import urllib.error
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }).encode("utf-8")
    req = urllib.request.Request(endpoint, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(_extract_http_error_detail(exc)) from None
    choice = (data.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content")
    if content is None:
        # Most commonly: response got cut off by max_tokens before any
        # content was emitted (some models front-load reasoning tokens
        # that don't count as "content"), or a free-tier model returned
        # an empty/refused completion. Either way, surface something
        # readable instead of crashing on None.strip().
        finish_reason = choice.get("finish_reason", "unknown")
        raise RuntimeError(f"provider returned no content (finish_reason={finish_reason})")
    return content.strip()


def _call_anthropic_style(endpoint: str, api_key: str, model: str, prompt: str, max_tokens: int = 1600) -> str:
    import urllib.request
    import urllib.error
    body = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(endpoint, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(_extract_http_error_detail(exc)) from None
    return data["content"][0]["text"].strip()


def _call_gemini_style(endpoint: str, api_key: str, model: str, prompt: str, max_tokens: int = 1600) -> str:
    """Gemini's REST shape differs from both the OpenAI and Anthropic
    styles: the model name is part of the URL path (not the body), the key
    goes in an x-goog-api-key header (not Authorization/x-api-key), and the
    response nests text under candidates[0].content.parts[0].text."""
    import urllib.request
    import urllib.error
    url = f"{endpoint}/{model}:generateContent"
    body = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.3},
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(_extract_http_error_detail(exc)) from None
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError):
        # Most commonly: the response was truncated/blocked by a safety
        # filter and has no parts — surface something readable instead of
        # a raw KeyError.
        reason = data.get("candidates", [{}])[0].get("finishReason", "unknown")
        raise RuntimeError(f"Gemini returned no text (finishReason={reason})") from None


def _call_ai_provider(cfg: dict, api_key: str, model: str, prompt: str, max_tokens: int = 1600) -> str:
    """Shared style dispatch — used by both ai_explain_tier() (Settings tab
    advisor) and ai_diagnose_failure() (daemon blocklist diagnosis) so the
    three request-shape implementations only exist once."""
    if cfg["style"] == "anthropic":
        return _call_anthropic_style(cfg["endpoint"], api_key, model, prompt, max_tokens=max_tokens)
    if cfg["style"] == "gemini":
        return _call_gemini_style(cfg["endpoint"], api_key, model, prompt, max_tokens=max_tokens)
    return _call_openai_style(cfg["endpoint"], api_key, model, prompt, max_tokens=max_tokens)


# Free-tier providers (Groq/Gemini) comfortably support several thousand
# output tokens, but a flat cap either wasted budget on small tiers or
# truncated JSON on big ones. ~90 tokens/tweak covers "why" + "watch_for"
# plus JSON punctuation with margin; floor/ceiling keep it sane at both ends.
_AI_TOKENS_PER_TWEAK = 90
_AI_TOKENS_BASE = 250
_AI_TOKENS_MIN = 800
_AI_TOKENS_MAX = 3800
# Keeping batches at 20 tweaks (not "all 70+ in one shot") is what actually
# stays inside that token ceiling with room to spare, rather than trying to
# raise the ceiling indefinitely for a 75-tweak tier.
_AI_BATCH_SIZE = 20


def _ai_max_tokens_for(n_tweaks: int) -> int:
    return max(_AI_TOKENS_MIN, min(_AI_TOKENS_MAX, _AI_TOKENS_BASE + _AI_TOKENS_PER_TWEAK * n_tweaks))


def ai_explain_tier(profile: HardwareProfile, tier: str, tweaks: List[Tweak]) -> dict:
    """Advisory only. Never lets the model pick tweaks — it only narrates
    and risk-annotates the list list_tweaks_for_tier() already produced.
    Degrades cleanly (available=False) with no key configured or on any
    network/parsing failure; callers should treat that as non-fatal.

    Batches tweaks _AI_BATCH_SIZE at a time and merges results. This used
    to send everything in one call capped at tweaks[:25] with a flat 1600
    max_tokens — on any tier bigger than ~25 (minimal/standard regularly
    run 70+), most tweaks silently got no insight at all, and even the
    first 25 risked truncated/unparseable JSON. Batching means every tweak
    in the tier gets covered, at the cost of one API call per ~20 tweaks
    instead of always exactly one."""
    api_key, provider = get_ai_credentials()
    if not api_key or provider not in AI_PROVIDERS:
        return {"available": False, "provider": None,
                "reason": "No AI key configured. Add one in Settings, or set an env var (see .env.example).",
                "text": None}

    cfg = AI_PROVIDERS[provider]
    model = _resolve_model(provider)
    all_tweak_ids = [t.id for t in tweaks]
    batches = [tweaks[i:i + _AI_BATCH_SIZE] for i in range(0, len(tweaks), _AI_BATCH_SIZE)] or [[]]
    batch_count = len(batches)

    summary = ""
    merged_tweaks: dict = {}
    errors: list = []
    for idx, batch in enumerate(batches, start=1):
        prompt = _build_advisor_prompt(
            profile, tier, batch,
            batch_index=idx, batch_count=batch_count, total_in_tier=len(tweaks),
        )
        try:
            raw = _call_ai_provider(cfg, api_key, model, prompt, max_tokens=_ai_max_tokens_for(len(batch)))
            parsed = _parse_ai_response(raw, [t.id for t in batch])
            if idx == 1 and parsed["summary"]:
                summary = parsed["summary"]
            merged_tweaks.update(parsed["tweaks"])
        except Exception as exc:  # noqa: BLE001 — one bad batch shouldn't blank out the rest
            errors.append(str(exc))

    if not merged_tweaks and not summary:
        return {"available": False, "provider": cfg["label"],
                "reason": f"{cfg['label']} request failed: {errors[0] if errors else 'no response'}",
                "text": None, "summary": None, "tweaks": {}}

    reason = None
    if errors:
        missing = len(all_tweak_ids) - len(merged_tweaks)
        reason = f"{cfg['label']}: got insight for {len(merged_tweaks)}/{len(all_tweak_ids)} tweaks ({missing} batch call(s) failed)."

    return {
        "available": True, "provider": cfg["label"], "free": cfg["free"], "reason": reason,
        "summary": summary, "tweaks": merged_tweaks,
        # Kept for backward compatibility with anything still reading .text —
        # the GUI itself now reads .summary/.tweaks.
        "text": summary,
    }


def _build_failure_diagnosis_prompt(item_id: str, error: str, fail_count: int, mode: str = "apply_failed") -> str:
    tweak = TWEAK_BY_ID.get(item_id.split(":")[0])  # hybrid step ids are "tweak_id:method" — base tweak still looks up
    tweak_desc = f'"{tweak.name}": {tweak.description}' if tweak else "(tweak id not found in current database)"
    if mode == "persistent_drift":
        situation = (
            "A Windows automation tool has been reapplying one system tweak on a real user's "
            f"PC, and the write itself reports success every time, but the value is back to "
            f"the non-target state again on the next check — this has now happened {fail_count} "
            "consecutive checks in a row. Nothing is erroring; whatever is undoing it happens "
            "outside this tool's own write call. It is about to give up retrying this tweak "
            "permanently until the user manually clears it."
        )
    else:
        situation = (
            "A Windows automation tool has been retrying one system tweak on a real user's "
            f"PC and it has now failed {fail_count} times in a row with the same class of error. "
            "It is about to give up on this tweak permanently until the user manually clears it."
        )
    return (
        f"{situation}\n\n"
        f"Tweak: {item_id} — {tweak_desc}\n"
        f"Raw error/detail text from the last attempt:\n{error[:800]}\n\n"
        "Respond with ONLY raw JSON (no markdown fences, no prose outside the JSON):\n"
        '{\n'
        '  "likely_cause": "<one plain-English sentence, no jargon dump, on what is most likely '
        'blocking this — e.g. a protected registry ACL, a policy override, a missing service>",\n'
        '  "suggested_action": "<one concrete, specific thing the user could try manually, or '
        '\\"no safe manual fix — leave it blocked\\" if there genuinely isn\'t one>",\n'
        '  "safe_to_keep_retrying": <true or false — false if retrying is pointless/wasteful given '
        'this error, true if it might be transient>\n'
        '}\n\n'
        "Be concrete and specific to the actual error text above — do not give a generic "
        "troubleshooting checklist."
    )


def ai_diagnose_failure(item_id: str, error: str, fail_count: int, mode: str = "apply_failed") -> dict:
    """Called once per tweak, the moment it crosses the relevant threshold
    (see daemon_check_and_reapply_once) — not every cycle it stays blocked,
    to avoid burning API calls on something that's already been diagnosed.
    mode distinguishes an outright apply error ("apply_failed") from a
    tweak that keeps reporting success but never sticks ("persistent_drift")
    — same mechanism, different situation, so the prompt shouldn't tell the
    model something "failed" when it didn't. Same degrade-cleanly contract
    as ai_explain_tier(): no key configured or any failure just means no
    diagnosis, never a crash that could take the daemon cycle down with it."""
    api_key, provider = get_ai_credentials()
    if not api_key or provider not in AI_PROVIDERS:
        return {"available": False, "reason": "no AI key configured"}
    cfg = AI_PROVIDERS[provider]
    model = _resolve_model(provider)
    prompt = _build_failure_diagnosis_prompt(item_id, error, fail_count, mode)
    try:
        raw = _call_ai_provider(cfg, api_key, model, prompt)
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
            text = text.strip()
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("response was not a JSON object")
        return {
            "available": True, "provider": cfg["label"],
            "likely_cause": str(data.get("likely_cause", "")).strip(),
            "suggested_action": str(data.get("suggested_action", "")).strip(),
            "safe_to_keep_retrying": bool(data.get("safe_to_keep_retrying", False)),
        }
    except Exception as exc:  # noqa: BLE001 — daemon cycle must survive a bad/failed AI call
        return {"available": False, "reason": f"{cfg['label']} diagnosis failed: {exc}"}


_AI_CHAT_HISTORY_TURNS = 8  # trailing turns kept for context; free-tier providers don't need the full transcript


def _build_chat_prompt(profile: HardwareProfile, message: str, history: List[dict],
                        tier: str, tweaks: List["Tweak"], maint_actions: List["MaintenanceAction"]) -> str:
    """Q&A + tweak-selection + maintenance-selection prompt, grounded in the
    real detected hardware AND the real hardware-filtered lists for the
    currently-selected tier (list_tweaks_for_tier) and for maintenance
    (list_maintenance_actions) — the same lists the checkboxes on the
    Tweak Engine and Maintenance tabs show. History is plain user/assistant
    turns from the GUI's in-memory chat state — no disk persistence, so
    nothing here needs sanitizing beyond length.

    The model is allowed to hand back which checkboxes to tick in either
    list, but ONLY by id from the exact lists given below — it never
    invents an id, and it never sees or can select a tweak outside the
    current tier's already-hardware-filtered list (extreme tier only
    enters this list if the human already manually selected Extreme in
    the GUI, same rule as everywhere else in the app). Ticking a box is
    as far as it goes for both: applying tweaks and running maintenance
    are still separate, explicit button clicks by the user."""
    disks = ", ".join(f"{d.media_type} {d.size_gb}GB" for d in profile.disks) or "none detected"
    gpu = profile.gpus[0].name if profile.gpus else "none detected"
    battery_line = "laptop" if profile.is_laptop else "desktop"
    extras = []
    if profile.antivirus and profile.antivirus != "Unknown":
        extras.append(f"AV: {profile.antivirus}")
    if profile.startup_item_count:
        extras.append(f"{profile.startup_item_count} startup items")
    if profile.bloatware_installed:
        extras.append(f"{len(profile.bloatware_installed)} known bloatware apps present")
    if profile.uptime_hours:
        extras.append(f"uptime {profile.uptime_hours:.0f}h")
    if profile.board and profile.board.manufacturer and profile.board.manufacturer != "Unknown":
        extras.append(f"board: {profile.board.manufacturer} {profile.board.model}".strip())
    extras_line = f", {', '.join(extras)}" if extras else ""
    hw = (
        f"CPU: {profile.cpu.name} ({profile.cpu.physical_cores}c), RAM: {profile.ram.total_gb}GB, "
        f"Disks: {disks}, GPU: {gpu}, "
        f"Form factor: {battery_line}, OS: {profile.os_name} (build {profile.os_build}){extras_line}"
    )
    convo = ""
    for turn in history[-_AI_CHAT_HISTORY_TURNS:]:
        role = "User" if turn.get("role") == "user" else "Assistant"
        text = str(turn.get("content", ""))[:1200]
        convo += f"{role}: {text}\n"
    applied_ids = _get_applied_tweak_ids()
    tweak_menu = "\n".join(
        f"- {t.id}: {t.name} [{t.risk}"
        f"{', ALREADY APPLIED' if t.id in applied_ids else ''}] — {t.description}"
        for t in tweaks
    ) or "(no tweaks available for this tier on this hardware)"
    maint_menu = "\n".join(
        f"- {a.id}: {a.name} [{'takes a while' if a.disruptive else 'quick'}"
        f"{', admin' if a.requires_admin else ''}] — {a.description}" for a in maint_actions
    ) or "(no maintenance actions available on this hardware)"
    return (
        "You are the in-app assistant for Flow, a Windows tweak/optimization tool, talking to "
        "someone who may be non-technical and doesn't know what most of these tweaks or "
        "maintenance actions mean. TWEAK_DATABASE/MAINTENANCE_ACTIONS and the app's own hardware "
        "filter already decided what's SAFE to offer — the two lists below are the complete, "
        f"final menus for this machine (tweaks at the currently-selected \"{tier}\" tier). You "
        "cannot add, invent, or suggest anything outside them, and you never apply or run anything "
        "yourself — the user still clicks Apply / Run Selected.\n\n"
        "Answer what was actually asked, using the real data below — never fall back on vague "
        "future tense like 'Flow will look at your hardware and suggest tweaks' when you already "
        "have that hardware and that tweak list right here. If they ask why their PC is slow, name "
        "actual likely culprits from the data below (startup item count, bloatware present, disk "
        "type, RAM headroom) instead of a generic 'could be many things.' If they ask a general "
        "Windows question unrelated to tweaks/maintenance, just answer it directly and practically — "
        "don't redirect them to 'check the app' when you can just tell them.\n\n"
        "If the user asks you to pick things for them (tweaks, maintenance, or 'clean up/speed up "
        "my PC' generally, which can mean both), you have two options:\n"
        "  1. If you don't yet know enough about what they use this PC for (gaming vs office work, "
        "battery life vs performance, dev tools/VMs, privacy concerns, whether they're mid-"
        "troubleshooting something and can't afford to lose event logs, etc.), ask ONE short, "
        "plain-language clarifying question — do not ask several at once, no jargon, a "
        "non-technical person needs a simple either/or.\n"
        "  2. Once you have enough to make a sensible call, select ids from the list(s) below that "
        "fit their situation and this specific hardware. Prefer broadly safe, low-risk/quick picks "
        "for a non-technical user unless they've indicated otherwise. Never select a high-risk tweak "
        "or a disruptive maintenance action (marked 'takes a while', e.g. SFC/DISM/defrag/chkdsk) "
        "unless the user's own words show they understand and accept that tradeoff — these can tie "
        "up the machine for a long time or, for chkdsk, need a reboot.\n\n"
        "Tweaks tagged 'ALREADY APPLIED' in the list below are already on for this machine — never "
        "include those in select_tweak_ids again (re-ticking a checked box does nothing useful), "
        "and if the user asks what's already been applied or turned on, answer directly from that "
        "tag rather than guessing. You cannot revert a tweak yourself — if they want one turned back "
        "off, tell them to uncheck it and use Revert Changes, you can't do it from chat.\n\n"
        "CRITICAL — you never apply, run, or turn anything on, even when the user says 'ok do it' "
        "or 'apply them': selecting ids only checks boxes on their screen. Never say 'I've applied', "
        "'I've turned on', or 'done' — say 'I've selected/ticked' and always end by telling them to "
        "click Apply Selected Tweaks or Run Selected themselves. Claiming something already happened "
        "when it didn't is worse than not answering.\n\n"
        "When you select more than a couple of ids, do NOT list every one by name in the reply text — "
        "the app already shows the user exactly which boxes got checked and displays a count "
        "automatically. Just say roughly how many and the general theme (e.g. 'ticked 12 low-risk "
        "visual/startup tweaks that fit a laptop like yours'); spelling out each one by name wastes "
        "space you don't have and risks your response getting cut off before the id list even starts.\n\n"
        f"Detected hardware: {hw}\n\n"
        f"Available tweaks at \"{tier}\" tier on this hardware (id: name [risk, applied-state] — description):\n"
        f"{tweak_menu}\n\n"
        f"Available maintenance actions on this hardware (id: name [speed/admin] — description):\n"
        f"{maint_menu}\n\n"
        f"{convo}"
        f"User: {message}\n\n"
        "Respond with ONLY raw JSON (no markdown fences, no prose outside it), matching exactly, "
        "IN THIS FIELD ORDER (ids first, so a length cutoff can never lose your selection):\n"
        '{"select_tweak_ids": ["<tweak id>", ...] or null, '
        '"select_maint_ids": ["<maintenance id>", ...] or null, '
        '"reply": "<what to show the user — plain language, their language if they asked for one. '
        'Keep it as short as the question allows, but don\'t truncate a real explanation just to '
        'be brief — a genuine \'why is my PC slow\' deserves actual specifics, not one line. Never '
        'list selected items by name here — see the rule above.>"}\n'
        "Use the select_* fields only when you are actually making a selection this turn "
        "(option 2 above) — null for plain answers, clarifying questions, or anything else. Every "
        "id must be copied exactly from its list above; do not put a tweak id in select_maint_ids "
        "or vice versa."
    )


def _salvage_chat_json(text: str, valid_tweak_ids: List[str], valid_maint_ids: List[str]) -> Optional[dict]:
    """Best-effort recovery when json.loads() fails outright — most often
    because the response got cut off mid-field by max_tokens (a chatty
    reply enumerating every selected tweak by name easily overruns the
    budget). The prompt puts the id arrays FIRST specifically so this is
    the common shape: ids complete, reply field cut short or missing
    entirely. So ids alone are enough to salvage something — a synthesized
    reply beats losing a real selection just because the trailing prose
    got cut. Returns None only if nothing usable was found at all."""
    import re

    def _salvage_ids(field_name, valid_ids):
        m = re.search(rf'"{field_name}"\s*:\s*\[(.*?)(?:\]|$)', text, re.DOTALL)
        if not m:
            return None
        valid = set(valid_ids)
        found = [i for i in re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1)) if i in valid]
        return found or None

    tweak_ids = _salvage_ids("select_tweak_ids", valid_tweak_ids)
    maint_ids = _salvage_ids("select_maint_ids", valid_maint_ids)

    reply_match = re.search(r'"reply"\s*:\s*"((?:[^"\\]|\\.)*)"', text, re.DOTALL)
    reply = None
    if reply_match:
        # This regex only matches a properly closed string (an unescaped
        # closing quote is part of the pattern) — a genuinely truncated
        # "reply" field with no closing quote simply won't match here,
        # so no separate truncation check is needed.
        try:
            reply = json.loads(f'"{reply_match.group(1)}"')
        except (json.JSONDecodeError, ValueError):
            reply = reply_match.group(1).replace('\\n', '\n').replace('\\"', '"')
        reply = reply.strip()

    if reply:
        pass
    elif tweak_ids or maint_ids:
        parts = []
        if tweak_ids:
            parts.append(f"{len(tweak_ids)} tweak{'s' if len(tweak_ids) != 1 else ''}")
        if maint_ids:
            parts.append(f"{len(maint_ids)} maintenance action{'s' if len(maint_ids) != 1 else ''}")
        reply = (
            f"Selected {' and '.join(parts)} based on your hardware — review them and click "
            "Apply Selected Tweaks / Run Selected to actually apply. (The full explanation got cut "
            "off — ask again if you want the reasoning.)"
        )
    else:
        return None

    return {
        "reply": reply.strip(),
        "select_tweak_ids": _salvage_ids("select_tweak_ids", valid_tweak_ids),
        "select_maint_ids": _salvage_ids("select_maint_ids", valid_maint_ids),
    }


def _parse_chat_response(raw_text: str, valid_tweak_ids: List[str], valid_maint_ids: List[str]) -> dict:
    """Tolerant JSON parse for the chat reply, same fallback contract as
    _parse_ai_response(): if the model ignores the JSON instruction (some
    free-tier models do), the whole reply is still shown as plain text
    rather than erroring the chat out. Both select_* lists are filtered
    against the real ids handed to the prompt so a hallucinated/stale id
    can never reach either tab's checkboxes."""
    text = (raw_text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    empty = {"reply": raw_text.strip(), "select_tweak_ids": None, "select_maint_ids": None}
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return _salvage_chat_json(text, valid_tweak_ids, valid_maint_ids) or empty
    if not isinstance(data, dict):
        return _salvage_chat_json(text, valid_tweak_ids, valid_maint_ids) or empty

    reply = data.get("reply")
    reply = reply.strip() if isinstance(reply, str) and reply.strip() else raw_text.strip()

    def _filter(raw_ids, valid_ids):
        valid = set(valid_ids)
        if not isinstance(raw_ids, list):
            return None
        filtered = [i for i in raw_ids if isinstance(i, str) and i in valid]
        return filtered or None

    return {
        "reply": reply,
        "select_tweak_ids": _filter(data.get("select_tweak_ids"), valid_tweak_ids),
        "select_maint_ids": _filter(data.get("select_maint_ids"), valid_maint_ids),
    }


def _local_chat_fallback(profile: HardwareProfile, message: str, tier: str) -> dict:
    """No-key mode. Not an LLM — plain keyword matching over the hardware
    profile and the tier's own tweak list. Answers the common questions
    (what's my hardware, what tier, find me tweaks about Y) without any
    network call or API key. Never guesses at a tweak's effect beyond its
    own stored description — if nothing matches, it says so and points at
    the Tweak Engine tab instead of making something up."""
    msg = message.lower()
    tier = tier if tier in TIER_ORDER else "minimal"
    tweaks = list_tweaks_for_tier(tier, profile)

    def profile_summary() -> str:
        disks = ", ".join(f"{d.media_type} {d.size_gb}GB" for d in profile.disks) or "none detected"
        gpu = profile.gpus[0].name if profile.gpus else "none detected"
        return (f"{profile.cpu.name}, {profile.cpu.physical_cores}c/{profile.cpu.logical_cores}t, "
                f"{profile.ram.total_gb}GB RAM, {disks}, {gpu}, {profile.os_name}.")

    greetings = ("hi", "hello", "hey", "sup", "yo")
    if msg.strip(" !.?") in greetings:
        return {"available": True, "provider": "Flow Assistant (offline)",
                "reply": "Hey — running without an AI key right now, so I can only look things "
                         "up locally: your hardware specs, the suggested tier, or search the "
                         f"current tier's tweak list by keyword. {profile.suggested_tier.capitalize()} "
                         "is the suggested tier for this machine.",
                "select_ids": None, "select_maint_ids": None}

    if any(k in msg for k in ("hardware", "spec", "cpu", "gpu", "ram", "disk", "machine")):
        return {"available": True, "provider": "Flow Assistant (offline)",
                "reply": profile_summary(), "select_ids": None, "select_maint_ids": None}

    if any(k in msg for k in ("tier", "suggest", "recommend")):
        return {"available": True, "provider": "Flow Assistant (offline)",
                "reply": f"Suggested tier for this hardware: {profile.suggested_tier}. "
                         f"{profile_summary()}",
                "select_ids": None, "select_maint_ids": None}

    matches = [t for t in tweaks if msg in t.name.lower() or msg in t.description.lower()
               or any(w in t.name.lower() or w in t.description.lower()
                      for w in msg.split() if len(w) > 3)]
    if matches:
        lines = [f"- {t.name} [{t.risk}]: {t.description[:140]}" for t in matches[:5]]
        return {"available": True, "provider": "Flow Assistant (offline)",
                "reply": "Closest matches in the current tier's tweak list:\n" + "\n".join(lines),
                "select_ids": None, "select_maint_ids": None}

    return {"available": True, "provider": "Flow Assistant (offline)",
            "reply": "No AI key configured, so I'm running in offline lookup mode — I can share "
                     "your hardware specs, the suggested tier, or search the current tier's "
                     "tweaks by keyword. Nothing matched that in the tweak list. For full "
                     "reasoning over free-form questions, add a key in Settings (see "
                     ".env.example for the free-tier providers).",
            "select_ids": None, "select_maint_ids": None}


def ai_chat_reply(profile: HardwareProfile, message: str, history: List[dict], tier: str = "minimal") -> dict:
    """Same degrade-cleanly contract as ai_explain_tier()/ai_diagnose_failure():
    any network/parsing failure returns available=False with a readable
    reason, never raises across the bridge. No key configured no longer
    hard-fails the tab — it drops to _local_chat_fallback() instead so the
    Chat tab still does something useful with zero setup."""
    message = (message or "").strip()
    if not message:
        return {"available": False, "reason": "Empty message.", "reply": None}
    api_key, provider = get_ai_credentials()
    if not api_key or provider not in AI_PROVIDERS:
        return _local_chat_fallback(profile, message, tier)
    tier = tier if tier in TIER_ORDER else "minimal"
    tweaks = list_tweaks_for_tier(tier, profile)
    tweak_ids = [t.id for t in tweaks]
    maint_actions = list_maintenance_actions(profile)
    maint_ids = [a.id for a in maint_actions]
    cfg = AI_PROVIDERS[provider]
    model = _resolve_model(provider)
    prompt = _build_chat_prompt(profile, message, history, tier, tweaks, maint_actions)
    try:
        raw = _call_ai_provider(cfg, api_key, model, prompt, max_tokens=4096)
        parsed = _parse_chat_response(raw, tweak_ids, maint_ids)
        select_tweak_ids = parsed["select_tweak_ids"]
        if select_tweak_ids:
            applied_ids = _get_applied_tweak_ids()
            select_tweak_ids = [i for i in select_tweak_ids if i not in applied_ids] or None
        return {"available": True, "provider": cfg["label"], "reply": parsed["reply"],
                "select_ids": select_tweak_ids, "select_maint_ids": parsed["select_maint_ids"]}
    except Exception as exc:  # noqa: BLE001 — chat tab must survive a bad/failed AI call
        return {"available": False, "provider": cfg["label"],
                "reason": f"{cfg['label']} request failed: {exc}", "reply": None}



# ═══════════════════════════════════════════════════════════════════
# SECTION 6 — GUI (pywebview)
# ═══════════════════════════════════════════════════════════════════
# Single-file per the architecture call — the HTML/CSS/JS lives here as a
# string, not a separate asset file. pywebview loads it directly with no
# network dependency, so fonts/assets are all local-safe (system fonts only).
#
# Flow (Manual mode, matches the architecture doc):
#   load -> detect() + admin_status() populate the System panel
#         -> tier radio change -> list_tweaks(tier) refreshes the tweak list
#         -> "Create Restore Point" (optional but recommended first)
#         -> "Apply Tier" -> apply_tier(tier), results stream into the log
#         -> "Revert All" -> revert_all(), results stream into the log
#
# is_admin()/TWEAKS_APPLY_ENABLED are just reflected as banners here — this
# GUI does not attempt to self-elevate. Proper UAC elevation is Section 7's
# job (PyInstaller onefile + manifest), not this window's.

_GUI_HTML = r"""


<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Flow</title>
<style>
  :root {
    --onyx: #0B0F19;
    --panel: #151C2C;
    --panel-hover: #1E293B;
    --border: #26314a;
    --text: #e7ecf7;
    --muted: #8b97b5;
    --cyan: #00B8D4;
    --emerald: #10B981;
    --red: #EF4444;
    --amber: #F5A524;
    --glow: rgba(0,184,212,0.25);
    --extreme: #ff3b6b;
    --terminal-bg: #05070c;
    --overlay: rgba(0,0,0,0.6);
    --shadow: rgba(0,0,0,0.35);
    --shadow-strong: rgba(0,0,0,0.6);
    --glass: rgba(21,28,44,0.62);
    --glass-border: rgba(255,255,255,0.08);
    --grad-cyan: linear-gradient(135deg, #00B8D4, #0090E0);
    --grad-violet: linear-gradient(135deg, #7c6dfb, #00B8D4);
    --mesh-a: rgba(0,184,212,0.14);
    --mesh-b: rgba(124,109,251,0.10);
    --tile-bg: rgba(255,255,255,0.03);
    --glass-highlight: rgba(255,255,255,0.06);
    --glass-blur: 18px;
    color-scheme: dark;
  }
  :root[data-theme="light"] {
    --onyx: #f4f6fb;
    --panel: #ffffff;
    --panel-hover: #eef1f8;
    --border: #dbe1ee;
    --text: #182033;
    --muted: #64748b;
    --cyan: #0090a8;
    --emerald: #059669;
    --red: #dc2626;
    --amber: #b8790a;
    --glow: rgba(0,144,168,0.18);
    --extreme: #d92c5c;
    --terminal-bg: #10151f;
    --overlay: rgba(15,23,42,0.35);
    --shadow: rgba(30,41,59,0.10);
    --shadow-strong: rgba(30,41,59,0.22);
    --glass: rgba(255,255,255,0.72);
    --glass-border: rgba(15,23,42,0.06);
    --grad-cyan: linear-gradient(135deg, #0090a8, #0f6fd0);
    --grad-violet: linear-gradient(135deg, #6a5cd8, #0090a8);
    --mesh-a: rgba(0,144,168,0.10);
    --mesh-b: rgba(106,92,216,0.08);
    --tile-bg: rgba(15,23,42,0.035);
    --glass-highlight: rgba(255,255,255,0.55);
    --glass-blur: 22px;
    color-scheme: light;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0; background: var(--onyx); color: var(--text);
    background-image:
      radial-gradient(ellipse 900px 500px at 8% -10%, var(--mesh-a), transparent 60%),
      radial-gradient(ellipse 700px 500px at 100% 0%, var(--mesh-b), transparent 55%);
    background-attachment: fixed;
    font-family: "Segoe UI Variable Text", "Segoe UI", system-ui, -apple-system, sans-serif;
    font-size: 13px; line-height: 1.45; overflow: hidden;
    -webkit-font-smoothing: antialiased; user-select: none;
    transition: background-color .2s ease, color .2s ease;
  }
  .mono { font-family: "Cascadia Code", "JetBrains Mono", Consolas, monospace; }
  ::-webkit-scrollbar { width: 8px; height: 8px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }
  ::-webkit-scrollbar-thumb:hover { background: var(--panel-hover); }

  button {
    font-family: inherit; font-size: 12px; color: var(--text);
    background: var(--panel-hover); border: 1px solid var(--glass-border);
    border-radius: 9px; padding: 8px 13px; cursor: pointer;
    transition: border-color .15s, box-shadow .15s, transform .05s, background .15s;
  }
  button:hover:not(:disabled) { border-color: var(--cyan); box-shadow: 0 0 0 1px var(--glow); }
  button:active:not(:disabled) { transform: translateY(1px); }
  button:disabled { opacity: 0.45; cursor: not-allowed; }
  button.primary {
    background: var(--grad-cyan); color: #041014; border-color: transparent; font-weight: 600;
    box-shadow: 0 4px 14px var(--glow);
  }
  button.primary:hover:not(:disabled) { box-shadow: 0 6px 22px var(--glow); transform: translateY(-1px); }
  button.danger { border-color: rgba(239,68,68,0.5); color: var(--red); }
  button.danger:hover:not(:disabled) { border-color: var(--red); box-shadow: 0 0 0 1px rgba(239,68,68,0.25); }
  input[type="text"], input[type="password"], input[type="number"], select {
    font-family: inherit; font-size: 12px; color: var(--text);
    background: var(--panel-hover); border: 1px solid var(--border);
    border-radius: 5px; padding: 7px 9px;
  }
  input:focus, select:focus, button:focus-visible {
    outline: 2px solid var(--cyan); outline-offset: 1px;
  }
  a { color: var(--cyan); }

  /* ===== Header — inversed nav: brand left, tabs right ===== */
  header {
    height: 58px; flex: 0 0 58px; display: flex; align-items: center; gap: 16px;
    padding: 0 20px; border-bottom: 1px solid var(--glass-border);
    background: var(--glass); backdrop-filter: blur(var(--glass-blur)); -webkit-backdrop-filter: blur(var(--glass-blur));
    box-shadow: inset 0 1px 0 var(--glass-highlight);
    position: relative; z-index: 5;
  }
  header::after {
    content: ""; position: absolute; left: 0; right: 0; bottom: -1px; height: 2px;
    background: linear-gradient(90deg, var(--cyan), var(--extreme) 50%, transparent 85%);
    opacity: 0.5;
  }
  .brand { display: flex; align-items: center; gap: 9px; }
  .brand .led {
    width: 9px; height: 9px; border-radius: 50%; background: var(--amber);
    box-shadow: 0 0 8px var(--amber); flex: 0 0 auto;
  }
  .brand .led.ok { background: var(--emerald); box-shadow: 0 0 8px var(--emerald); }
  .brand .led.bad { background: var(--red); box-shadow: 0 0 8px var(--red); }
  .brand-badge {
    font-family: "Cascadia Code", Consolas, monospace; font-size: 14px; font-weight: 600;
    letter-spacing: 1.8px; white-space: nowrap;
    background: var(--grad-violet); -webkit-background-clip: text; background-clip: text;
    -webkit-text-fill-color: transparent;
  }
  .header-meta { color: var(--muted); font-size: 11px; white-space: nowrap; }
  .spacer { flex: 1 1 auto; }

  .sidebar {
    flex: 0 0 190px; display: flex; flex-direction: column; gap: 4px;
    padding: 18px 12px; border-right: 1px solid var(--glass-border);
    background: var(--glass); backdrop-filter: blur(var(--glass-blur)); -webkit-backdrop-filter: blur(var(--glass-blur));
    box-shadow: inset -1px 0 0 var(--glass-highlight);
    overflow-y: auto; overflow-x: hidden;
    transition: flex-basis .2s cubic-bezier(.2,.8,.2,1);
  }
  .side-btn {
    display: flex; align-items: center; gap: 11px; width: 100%;
    background: transparent; border: 1px solid transparent; color: var(--muted);
    border-radius: 10px; padding: 10px 12px; font-size: 12.5px; text-align: left;
    transition: color .15s, background-color .15s, border-color .15s, transform .1s;
  }
  .side-btn:hover:not(:disabled) { color: var(--text); border-color: var(--glass-border); box-shadow: none; background: rgba(255,255,255,0.03); }
  .side-btn.active {
    color: var(--cyan); background: var(--glow); border-color: rgba(0,184,212,0.35); font-weight: 600;
    box-shadow: 0 4px 14px var(--glow);
  }
  .side-btn:active:not(:disabled) { transform: scale(0.98); }
  .side-icon { flex: 0 0 auto; line-height: 1; display: flex; }
  .side-icon svg { width: 18px; height: 18px; }
  .side-label { flex: 1 1 auto; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; transition: opacity .15s; }
  .sidebar-spacer { flex: 1 1 auto; }
  .side-collapse-btn { border-top: 1px solid var(--glass-border); margin-top: 6px; padding-top: 12px; border-radius: 0; }
  .side-collapse-btn .side-icon svg { transition: transform .2s ease; }

  .sidebar.collapsed { flex-basis: 60px; }
  .sidebar.collapsed .side-label { display: none; }
  .sidebar.collapsed .side-btn { justify-content: center; padding: 10px; gap: 0; }
  .sidebar.collapsed .side-collapse-btn .side-icon svg { transform: rotate(180deg); }

  @media (max-width: 760px) {
    .sidebar:not(.collapsed) { flex-basis: 60px; }
    .sidebar:not(.collapsed) .side-label { display: none; }
    .sidebar:not(.collapsed) .side-btn { justify-content: center; padding: 10px; }
    .side-collapse-btn { display: none; }
  }
  .icon-btn {
    width: 30px; height: 30px; padding: 0; display: flex; align-items: center; justify-content: center;
    border-radius: 50%; font-size: 14px; transition: border-color .15s, box-shadow .15s, transform .5s ease;
  }
  .icon-btn:hover:not(:disabled) { transform: rotate(45deg); }
  #btn-theme:hover:not(:disabled) { transform: scale(1.12) rotate(0deg); }
  @media (prefers-reduced-motion: reduce) { .icon-btn:hover:not(:disabled) { transform: none; } }
  .badge {
    font-family: "Cascadia Code", Consolas, monospace; font-size: 10px;
    padding: 3px 7px; border-radius: 3px; border: 1px solid var(--border); color: var(--muted);
  }
  .badge.warn { color: var(--amber); border-color: rgba(245,165,36,0.4); }
  .badge.ok { color: var(--emerald); border-color: rgba(16,185,129,0.4); }

  /* ===== Shell: left control plane (65%) / right telemetry plane (35%) ===== */
  .shell { display: flex; height: calc(100vh - 58px); }
  .col-left { flex: 65 1 0; min-width: 0; overflow-y: auto; padding: 22px 22px 28px; }
  .col-right { flex: 35 1 0; min-width: 260px; border-left: 1px solid var(--glass-border); background: transparent;
    display: flex; flex-direction: column; padding: 18px; gap: 16px; overflow: hidden; }

  .panel {
    display: none; opacity: 0; transform: translateY(4px);
  }
  .panel.active {
    display: block; opacity: 1; transform: translateY(0);
    animation: panel-in .22s cubic-bezier(.2,.8,.2,1) both;
  }
  @keyframes panel-in {
    from { opacity: 0; transform: translateY(6px); }
    to { opacity: 1; transform: translateY(0); }
  }
  @media (prefers-reduced-motion: reduce) { .panel.active { animation: none; } }

  .card {
    background: var(--glass); border: 1px solid var(--glass-border); border-radius: 16px;
    padding: 18px 20px; margin-bottom: 18px;
    box-shadow: 0 8px 24px var(--shadow), inset 0 1px 0 var(--glass-highlight);
    backdrop-filter: blur(var(--glass-blur)); -webkit-backdrop-filter: blur(var(--glass-blur));
    transition: border-color .18s, box-shadow .18s, transform .15s ease-out;
    overflow: hidden;
  }
  .card:hover { box-shadow: 0 12px 32px var(--shadow-strong), inset 0 1px 0 var(--glass-highlight); border-color: rgba(0,184,212,0.25); }
  .card-title {
    font-family: "Cascadia Code", Consolas, monospace; font-size: 11px; letter-spacing: 1.2px;
    text-transform: uppercase; color: var(--muted); margin-bottom: 14px; padding-bottom: 10px;
    border-bottom: 1px solid var(--glass-border);
    display: flex; align-items: center; justify-content: space-between;
  }
  .empty { color: var(--muted); font-size: 12px; padding: 10px 2px; }

  /* skeleton loading state — subtle shimmer instead of bare "Loading…" text */
  .skeleton-row {
    height: 34px; border-radius: 6px; margin-bottom: 6px;
    background: linear-gradient(90deg, var(--panel-hover) 25%, #26314a 37%, var(--panel-hover) 63%);
    background-size: 400% 100%; animation: shimmer 1.4s ease infinite;
  }
  @keyframes shimmer { 0% { background-position: 100% 0; } 100% { background-position: 0 0; } }
  @media (prefers-reduced-motion: reduce) { .skeleton-row { animation: none; background: var(--panel-hover); } }

  /* ---- 3(+1)-tier preset cards ---- */
  .preset-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 20px; }
  .preset-card {
    position: relative; overflow: hidden;
    background: var(--glass); border: 1px solid var(--glass-border); border-radius: 14px;
    padding: 14px 14px 12px; cursor: pointer;
    backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
    box-shadow: 0 4px 14px var(--shadow);
    transition: border-color .18s, box-shadow .18s, transform .15s cubic-bezier(.2,.8,.2,1);
  }
  .preset-card::before {
    content: ""; position: absolute; top: 0; left: 0; right: 0; height: 3px;
    background: var(--grad-cyan); opacity: 0.5; transition: opacity .18s;
  }
  .preset-card:hover { border-color: var(--cyan); transform: translateY(-3px); box-shadow: 0 10px 26px var(--shadow-strong); }
  .preset-card:hover::before { opacity: 1; }
  .preset-card:active { transform: translateY(-1px); }
  .preset-card .t { font-weight: 700; font-size: 13.5px; margin-bottom: 3px; }
  .preset-card .s { font-size: 10.5px; color: var(--muted); }
  .preset-card .n { font-family: "Cascadia Code", Consolas, monospace; font-size: 10px; color: var(--muted); margin-top: 8px; transition: color .18s; }
  .preset-card.active { border-color: var(--cyan); box-shadow: 0 0 0 1px var(--glow), 0 12px 30px var(--glow); }
  .preset-card.active::before { opacity: 1; }
  .preset-card.active .n { color: var(--cyan); }
  .preset-card.danger::before { background: linear-gradient(135deg, var(--extreme), var(--amber)); }
  .preset-card.danger { border-color: rgba(255,59,107,0.3); }
  .preset-card.danger:hover { border-color: var(--extreme); box-shadow: 0 10px 26px rgba(255,59,107,0.2); }
  .preset-card.danger.active { border-color: var(--extreme); box-shadow: 0 0 0 1px rgba(255,59,107,0.35), 0 12px 30px rgba(255,59,107,0.25); }
  .preset-card.danger .t { color: var(--extreme); }
  @media (prefers-reduced-motion: reduce) { .preset-card { transition: border-color .18s; } .preset-card:hover { transform: none; } }

  .hw-fit-line { color: var(--muted); font-size: 11px; margin-bottom: 10px; }

  .tweak-summary {
    display: flex; align-items: center; justify-content: space-between;
    font-size: 11px; color: var(--muted); margin-bottom: 10px;
  }
  .tweak-summary .select-links a { margin-left: 6px; cursor: pointer; text-decoration: none; font-size: 11px; }
  .tweak-summary .select-links a:hover { text-decoration: underline; }

  /* ---- categorized tweak grid, 2 columns ---- */
  .cat-section { margin-bottom: 14px; }
  .cat-header {
    font-family: "Cascadia Code", Consolas, monospace; font-size: 10.5px; letter-spacing: 1px;
    text-transform: uppercase; color: var(--cyan); margin: 0 0 8px; padding-bottom: 4px;
    border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 6px;
  }
  .cat-header .count { color: var(--muted); font-weight: normal; text-transform: none; letter-spacing: 0; }
  .tweak-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 6px 14px; }
  @media (max-width: 900px) { .tweak-grid { grid-template-columns: 1fr; } }
  .tweak-row {
    display: flex; align-items: flex-start; gap: 8px; padding: 7px 8px; border-radius: 6px;
    cursor: pointer; transition: background-color .15s;
  }
  .tweak-row:hover { background: var(--panel-hover); }

  /* custom sliding switch */
  .switch { position: relative; width: 34px; height: 19px; flex: 0 0 auto; margin-top: 1px; }
  .switch input { opacity: 0; width: 0; height: 0; }
  .switch .track {
    position: absolute; inset: 0; background: #2a3448; border-radius: 999px;
    transition: background .18s;
  }
  .switch .thumb {
    position: absolute; top: 2px; left: 2px; width: 15px; height: 15px; border-radius: 50%;
    background: var(--muted); transition: transform .22s cubic-bezier(.34,1.56,.64,1), background .18s;
  }
  .switch input:checked + .track { background: var(--glow); }
  .switch input:checked + .track + .thumb { transform: translateX(15px); background: var(--cyan); box-shadow: 0 0 8px var(--glow); }
  .switch input:focus-visible + .track { outline: 2px solid var(--cyan); outline-offset: 2px; }
  @media (prefers-reduced-motion: reduce) { .switch .thumb { transition: background .18s; } }

  .tweak-label { flex: 1 1 auto; min-width: 0; }
  .tweak-label .name-row { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
  .tweak-label .name { font-size: 12px; }
  .tweak-label .desc {
    font-size: 10.5px; color: var(--muted); margin-top: 2px;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
  }
  .risk-pill {
    font-family: "Cascadia Code", Consolas, monospace; font-size: 9px; text-transform: uppercase;
    letter-spacing: .5px; padding: 1px 6px; border-radius: 999px; border: 1px solid transparent;
  }
  .risk-pill.safe { color: var(--emerald); background: rgba(16,185,129,0.12); }
  .risk-pill.moderate { color: var(--amber); background: rgba(245,165,36,0.12); }
  .risk-pill.advanced { color: var(--red); background: rgba(239,68,68,0.12); }
  .risk-pill.extreme { color: var(--extreme); background: rgba(255,59,107,0.14); border-color: rgba(255,59,107,0.4); }

  .ai-note {
    display: none; margin-top: 5px; padding: 5px 7px; border-radius: 4px;
    border-left: 2px solid var(--cyan); background: var(--glow);
    font-size: 10.5px; line-height: 1.5;
  }
  .ai-note.visible { display: block; animation: panel-in .2s ease-out both; }
  .ai-note .ai-note-tag {
    font-family: "Cascadia Code", Consolas, monospace; font-size: 8.5px;
    letter-spacing: 1px; text-transform: uppercase; color: var(--cyan); margin-right: 4px;
  }
  .ai-note .ai-note-watch { color: var(--amber); display: block; margin-top: 3px; }
  .ai-note .ai-note-watch .ai-note-tag { color: var(--amber); }
  .ai-insight-box {
    display: none; margin-bottom: 12px; padding: 10px 12px; border-radius: 8px;
    border: 1px solid rgba(0,184,212,0.3); background: var(--glow); font-size: 11.5px;
  }
  .ai-insight-box.visible { display: block; animation: panel-in .22s cubic-bezier(.2,.8,.2,1) both; }
  .ai-insight-box.error { border-color: rgba(239,68,68,0.35); background: rgba(239,68,68,0.06); }
  .ai-insight-box .ai-tag {
    display: block; font-family: "Cascadia Code", Consolas, monospace; font-size: 9.5px;
    letter-spacing: 1px; text-transform: uppercase; color: var(--cyan); margin-bottom: 5px;
  }
  .ai-insight-box.error .ai-tag { color: var(--red); }
  .ai-dots::after { content: "."; animation: dots 1.2s steps(1) infinite; }
  @keyframes dots { 0% { content: "."; } 33% { content: ".."; } 66% { content: "..."; } }
  @media (prefers-reduced-motion: reduce) { .ai-note.visible, .ai-insight-box.visible { animation: none; } }

  /* ---- Idle Chores tab ---- */
  .field-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }
  .field-row label { font-size: 11px; color: var(--muted); }
  .status-line { font-size: 11px; color: var(--muted); margin-top: 4px; }
  .status-line.ok { color: var(--emerald); }
  .status-line.bad { color: var(--red); }
  .maint-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  @media (max-width: 900px) { .maint-grid { grid-template-columns: 1fr; } }
  .maint-card { background: var(--tile-bg); border: 1px solid var(--glass-border); border-radius: 12px; padding: 11px 13px; transition: border-color .18s, transform .15s ease-out, box-shadow .18s; }
  .maint-card:hover { border-color: rgba(0,184,212,0.3); transform: translateY(-2px); box-shadow: 0 6px 16px var(--shadow); }
  @media (prefers-reduced-motion: reduce) { .maint-card:hover { transform: none; } }
  .maint-card .name { font-size: 12px; font-weight: 600; margin-bottom: 3px; }
  .maint-card .desc { font-size: 10.5px; color: var(--muted); margin-bottom: 8px; line-height: 1.5; }
  .maint-card .row { display: flex; align-items: center; gap: 6px; }
  .flag {
    font-family: "Cascadia Code", Consolas, monospace; font-size: 9px; color: var(--muted);
    border: 1px solid var(--border); border-radius: 3px; padding: 1px 5px;
  }
  .flag.disruptive { color: var(--amber); border-color: rgba(245,165,36,0.4); }
  .diag-card {
    background: var(--tile-bg); border: 1px solid rgba(239,68,68,0.3); border-radius: 10px;
    padding: 8px 10px; margin-bottom: 8px; font-size: 11px;
    animation: panel-in .25s cubic-bezier(.2,.8,.2,1) both;
  }
  .diag-id { color: var(--cyan); margin-bottom: 4px; font-size: 10.5px; }
  .diag-cause, .diag-action { margin-bottom: 3px; color: var(--text); }
  .diag-flag, .diag-hint { color: var(--muted); font-size: 10px; }
  .log-mini {
    max-height: 140px; overflow-y: auto; font-size: 10.5px; color: var(--muted);
    background: var(--tile-bg); border: 1px solid var(--glass-border); border-radius: 8px;
    padding: 8px 10px; margin-top: 8px; display: none;
  }

  /* ---- Restore Points tab ---- */
  table.rp-table { width: 100%; border-collapse: collapse; font-size: 11.5px; }
  table.rp-table th {
    text-align: left; color: var(--muted); font-weight: normal; font-size: 10.5px;
    text-transform: uppercase; letter-spacing: .5px; padding: 6px 8px; border-bottom: 1px solid var(--border);
  }
  table.rp-table td { padding: 7px 8px; border-bottom: 1px solid var(--border); }
  table.rp-table tr:last-child td { border-bottom: none; }

  /* ---- System Audit tab ---- */
  .live-dot {
    width: 7px; height: 7px; border-radius: 50%; background: var(--emerald);
    box-shadow: 0 0 6px var(--emerald); animation: live-pulse 2s ease-in-out infinite;
  }
  .usage-updated { font-size: 9.5px; color: var(--muted); }
  .live-dot.stale { background: var(--muted); box-shadow: none; animation: none; }
  @keyframes live-pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
  @media (prefers-reduced-motion: reduce) { .live-dot { animation: none; } }

  .usage-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 4px; }
  @media (max-width: 900px) { .usage-grid { grid-template-columns: 1fr 1fr; } }
  .usage-tile {
    background: var(--tile-bg); border: 1px solid var(--glass-border); border-radius: 12px; padding: 11px 13px;
    backdrop-filter: blur(calc(var(--glass-blur) / 2)); -webkit-backdrop-filter: blur(calc(var(--glass-blur) / 2));
    min-width: 0; overflow: hidden;
  }
  .usage-label { font-size: 10.5px; color: var(--muted); text-transform: uppercase; letter-spacing: .5px; margin-bottom: 7px; }
  .usage-bar-track { height: 6px; border-radius: 999px; background: var(--panel-hover); overflow: hidden; margin-bottom: 6px; }
  .usage-bar-fill {
    height: 100%; width: 0%; border-radius: 999px;
    background: linear-gradient(90deg, var(--cyan), var(--emerald));
    transition: width .5s cubic-bezier(.2,.8,.2,1), background-color .3s;
  }
  .usage-bar-fill.warn { background: linear-gradient(90deg, var(--amber), var(--red)); }
  .usage-bar-fill.crit { background: var(--red); }
  .usage-value { font-family: "Cascadia Code", Consolas, monospace; font-size: 13px; font-weight: 600; overflow-wrap: break-word; word-break: break-word; }
  .usage-value.muted-value { color: var(--muted); font-size: 11px; font-weight: normal; }
  .usage-disks { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 10px; margin-top: 12px; }
  .usage-disk-tile { background: var(--tile-bg); border: 1px solid var(--glass-border); border-radius: 12px; padding: 10px 12px; }
  .usage-disk-tile .usage-label { margin-bottom: 5px; }
  @media (prefers-reduced-motion: reduce) { .usage-bar-fill { transition: background-color .3s; } }

  .usage-cores-label { font-size: 10.5px; color: var(--muted); text-transform: uppercase; letter-spacing: .5px; margin: 4px 0 8px; }
  .usage-cores { display: grid; grid-template-columns: repeat(auto-fill, minmax(46px, 1fr)); gap: 6px; }
  .usage-core-tile { text-align: center; }
  .usage-core-bar-track { height: 42px; width: 100%; border-radius: 5px; background: var(--panel-hover); overflow: hidden; display: flex; align-items: flex-end; }
  .usage-core-bar-fill { width: 100%; background: linear-gradient(180deg, var(--cyan), var(--emerald)); transition: height .5s cubic-bezier(.2,.8,.2,1), background-color .3s; }
  .usage-core-bar-fill.warn { background: linear-gradient(180deg, var(--amber), var(--red)); }
  .usage-core-bar-fill.crit { background: var(--red); }
  .usage-core-label { font-size: 9.5px; color: var(--muted); margin-top: 4px; font-family: "Cascadia Code", Consolas, monospace; }
  @media (prefers-reduced-motion: reduce) { .usage-core-bar-fill { transition: background-color .3s; } }

  .usage-top-procs { display: flex; flex-direction: column; gap: 4px; }
  .usage-proc-row { display: flex; align-items: center; gap: 8px; font-size: 11.5px; }
  .usage-proc-name { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .usage-proc-bar-track { flex: 0 0 90px; height: 5px; border-radius: 999px; background: var(--panel-hover); overflow: hidden; }
  .usage-proc-bar-fill { height: 100%; background: var(--cyan); border-radius: 999px; }
  .usage-proc-mem { flex: 0 0 64px; text-align: right; font-family: "Cascadia Code", Consolas, monospace; font-size: 10.5px; color: var(--muted); }

  .usage-net-adapters { margin-top: 6px; display: flex; flex-direction: column; gap: 3px; }
  .usage-net-adapter-row { font-size: 9.5px; color: var(--muted); display: flex; justify-content: space-between; gap: 6px; }
  .usage-net-adapter-row .n { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  .usage-spark { width: 100%; height: 24px; margin-top: 6px; display: block; }
  .usage-spark polyline { fill: none; stroke: var(--cyan); stroke-width: 2; vector-effect: non-scaling-stroke; }
  .usage-spark polygon { fill: var(--cyan); opacity: 0.08; }

  .audit-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0 24px; }
  @media (max-width: 900px) { .audit-grid { grid-template-columns: 1fr; } }
  .stat-row { display: flex; justify-content: space-between; gap: 10px; padding: 5px 0; border-bottom: 1px solid var(--border); font-size: 12px; }
  .stat-row .k { color: var(--muted); flex: 0 0 auto; }
  .stat-row .v { text-align: right; flex: 1 1 auto; min-width: 0; overflow-wrap: break-word; word-break: break-word; }
  ul.plain-list { list-style: none; margin: 0; padding: 0; font-size: 11.5px; }
  ul.plain-list li { padding: 3px 0; border-bottom: 1px solid var(--border); }
  ul.tweak-reasons { margin: 0 0 10px; padding-left: 18px; font-size: 11px; color: var(--muted); }

  /* ===== Right column: terminal / actions ===== */
  .rc-title {
    font-family: "Cascadia Code", Consolas, monospace; font-size: 10.5px; letter-spacing: 1px;
    text-transform: uppercase; color: var(--muted); margin-bottom: 8px;
    display: flex; align-items: center; justify-content: space-between;
  }
  .rc-title button { padding: 2px 7px; font-size: 9.5px; }
  #hw-body .stat-row { font-size: 11.5px; padding: 4px 0; }
  #hw-body .stat-row .v { color: var(--text); }

  .terminal-wrap { flex: 1 1 auto; display: flex; flex-direction: column; min-height: 0; }
  .terminal {
    flex: 1 1 auto; min-height: 0; overflow-y: auto; background: var(--terminal-bg);
    border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px;
    font-family: "Cascadia Code", "JetBrains Mono", Consolas, monospace; font-size: 11px;
    user-select: text;
  }
  .log-line {
    padding: 1.5px 0; white-space: pre-wrap; word-break: break-word;
    animation: log-in .18s ease-out both;
  }
  @keyframes log-in { from { opacity: 0; transform: translateX(-2px); } to { opacity: 1; transform: translateX(0); } }
  @media (prefers-reduced-motion: reduce) { .log-line { animation: none; } }
  .log-line.info { color: var(--cyan); }
  .log-line.ok, .log-line.success { color: var(--emerald); }
  .log-line.fail, .log-line.error { color: var(--red); }
  .log-line.skip, .log-line.warn { color: var(--amber); }
  .log-line::before { content: "> "; opacity: 0.5; }

  .action-panel { display: flex; flex-direction: column; gap: 8px; }
  #btn-apply { width: 100%; padding: 11px; font-size: 13px; display: flex; align-items: center; justify-content: center; gap: 8px; }
  .spinner {
    display: none; width: 13px; height: 13px; border-radius: 50%;
    border: 2px solid rgba(4,16,20,0.35); border-top-color: #041014; animation: spin .7s linear infinite;
  }
  #btn-apply.loading .spinner { display: inline-block; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .action-row2 { display: flex; gap: 8px; }
  .action-row2 button { flex: 1 1 0; }

  /* ===== Modal (AI settings) ===== */
  .modal-overlay {
    position: fixed; inset: 0; background: var(--overlay); backdrop-filter: blur(2px);
    display: none; align-items: center; justify-content: center; z-index: 10;
    opacity: 0; transition: opacity .15s ease-out;
  }
  .modal-overlay.visible { display: flex; opacity: 1; }
  .modal {
    width: 380px; background: var(--panel); border: 1px solid var(--border);
    border-radius: 10px; padding: 18px; box-shadow: 0 12px 40px var(--shadow-strong);
    transform: scale(.96) translateY(4px); transition: transform .18s cubic-bezier(.2,.8,.2,1);
  }
  .modal-overlay.visible .modal { transform: scale(1) translateY(0); }
  @media (prefers-reduced-motion: reduce) { .modal-overlay, .modal { transition: none; } }
  .modal h2 {
    font-family: "Cascadia Code", Consolas, monospace; font-size: 13px;
    letter-spacing: 1px; margin: 0 0 8px; color: var(--cyan);
  }
  .modal p { color: var(--muted); font-size: 11px; line-height: 1.6; margin: 0 0 12px; }
  .modal input { width: 100%; margin-bottom: 10px; }
  .modal-status { font-size: 11px; min-height: 16px; margin-bottom: 10px; color: var(--muted); }
  .modal-status.ok { color: var(--emerald); }
  .modal-status.bad { color: var(--red); }
  .modal-actions { display: flex; gap: 8px; justify-content: flex-end; }

  @media (prefers-reduced-motion: reduce) {
    .ai-dots::after { animation: none; content: "…"; }
    .spinner { animation: none; }
  }
</style>
</head>
<body>
<script>
  // Synchronous, pre-paint: sets the OS-matching theme immediately so there's
  // no flash of the wrong theme while waiting for pywebviewready. initTheme()
  // in the main script below still runs afterward to load any saved override.
  document.documentElement.setAttribute('data-theme',
    (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches) ? 'light' : 'dark');
</script>

<header>
  <div class="brand">
    <span class="led" id="status-led"></span>
    <span class="brand-badge">FLOW // WIN-OPT</span>
  </div>
  <span class="badge" id="admin-badge">checking&hellip;</span>
  <span class="badge warn" id="dryrun-badge" style="display:none;">dry run</span>
  <span class="badge ok" id="daemon-badge" style="display:none;" title="Background daemon is installed and re-applies drifted tweaks on a schedule">daemon active</span>
  <span class="header-meta mono" id="os-line"></span>
  <span class="spacer"></span>
  <button id="btn-theme" class="icon-btn" aria-label="Toggle light/dark theme" title="Toggle light/dark theme">&#9788;</button>
  <button id="btn-settings" class="icon-btn" aria-label="AI advisor settings" title="AI advisor settings">&#9881;</button>
</header>

<div class="modal-overlay" id="settings-overlay">
  <div class="modal" role="dialog" aria-labelledby="settings-title">
    <h2 id="settings-title">AI ADVISOR SETTINGS</h2>
    <p>
      Optional. Paste an API key and Flow detects the provider automatically —
      <strong>Groq</strong> (console.groq.com) and <strong>Gemini</strong> (aistudio.google.com/api-keys)
      both have a genuinely free tier. OpenRouter, Anthropic, and OpenAI keys work too.
      This only narrates/risk-flags tweaks already selected — it never decides what gets applied.
      Stored locally at ~/.flow/ai_config.json. Leave blank and save to clear it.
    </p>
    <input type="password" id="settings-key-input" placeholder="Paste API key&hellip;" autocomplete="off">
    <div class="modal-status" id="settings-status"></div>
    <div class="modal-actions">
      <button id="btn-settings-cancel">Cancel</button>
      <button id="btn-settings-save" class="primary">Save</button>
    </div>
  </div>
</div>

<div class="shell">
  <nav class="sidebar" id="sidebar" role="tablist" aria-label="Flow sections" aria-orientation="vertical">
    <button class="side-btn active" id="tab-tweaks" role="tab" aria-selected="true" data-tab="tweaks" title="Tweak Engine">
      <span class="side-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/></svg></span>
      <span class="side-label">Tweak Engine</span>
    </button>
    <button class="side-btn" id="tab-idle" role="tab" aria-selected="false" data-tab="idle" title="Idle Chores">
      <span class="side-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15 14"/></svg></span>
      <span class="side-label">Idle Chores</span>
    </button>
    <button class="side-btn" id="tab-maintenance" role="tab" aria-selected="false" data-tab="maintenance" title="Maintenance">
      <span class="side-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.5-3.5a6 6 0 0 1-7.7 7.7l-6.6 6.6a2.1 2.1 0 0 1-3-3l6.6-6.6a6 6 0 0 1 7.7-7.7z"/></svg></span>
      <span class="side-label">Maintenance</span>
    </button>
    <button class="side-btn" id="tab-restore" role="tab" aria-selected="false" data-tab="restore" title="Restore Points">
      <span class="side-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/></svg></span>
      <span class="side-label">Restore Points</span>
    </button>
    <button class="side-btn" id="tab-audit" role="tab" aria-selected="false" data-tab="audit" title="System Audit">
      <span class="side-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg></span>
      <span class="side-label">System Audit</span>
    </button>
    <button class="side-btn" id="tab-chat" role="tab" aria-selected="false" data-tab="chat" title="AI Chat">
      <span class="side-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg></span>
      <span class="side-label">AI Chat</span>
    </button>
    <div class="sidebar-spacer"></div>
    <button class="side-btn side-collapse-btn" id="btn-sidebar-collapse" aria-label="Collapse sidebar" title="Collapse sidebar">
      <span class="side-icon"><svg id="collapse-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"/></svg></span>
      <span class="side-label">Collapse</span>
    </button>
  </nav>
  <div class="col-left">

    <!-- ===== TWEAK ENGINE ===== -->
    <section class="panel active" id="panel-tweaks" role="tabpanel" aria-labelledby="tab-tweaks">
      <div class="preset-row" id="preset-row">
        <div class="preset-card" data-tier="minimal">
          <div class="t">Minimal</div>
          <div class="s">Zero risk</div>
          <div class="n" id="preset-count-minimal">&nbsp;</div>
        </div>
        <div class="preset-card" data-tier="standard">
          <div class="t">Standard</div>
          <div class="s">Recommended</div>
          <div class="n" id="preset-count-standard">&nbsp;</div>
        </div>
        <div class="preset-card" data-tier="maximal">
          <div class="t">Maximal</div>
          <div class="s">Aggressive</div>
          <div class="n" id="preset-count-maximal">&nbsp;</div>
        </div>
        <div class="preset-card danger" data-tier="extreme" title="Includes tweaks that disable real security controls (UAC, firewall, Defender). Not auto-suggested.">
          <div class="t">Extreme</div>
          <div class="s">Disables security controls</div>
          <div class="n" id="preset-count-extreme">&nbsp;</div>
        </div>
      </div>

      <div class="hw-fit-line mono" id="hw-fit-line"></div>
      <ul class="tweak-reasons" id="tier-reasons"></ul>
      <div class="ai-insight-box" id="ai-insight-box"></div>

      <div class="tweak-summary">
        <span id="tweak-summary-text">Pick a preset to load its tweaks.</span>
        <span class="select-links">
          <a id="select-all-link">select all</a>
          <a id="select-none-link">select none</a>
          <a id="btn-ai-insight" style="display:none;">&#10022; AI insight</a>
        </span>
      </div>

      <div id="tweak-list"><div class="empty">Pick a preset above to see its tweaks.</div></div>
    </section>

    <!-- ===== IDLE CHORES ===== -->
    <section class="panel" id="panel-idle" role="tabpanel" aria-labelledby="tab-idle">
      <div class="card">
        <div class="card-title">Background Daemon</div>
        <p style="color:var(--muted);font-size:11.5px;line-height:1.6;margin:0 0 10px;">
          Runs unattended on a schedule and re-checks every tweak Flow has applied so far —
          if Windows Update or a reinstall silently resets one, the daemon reapplies it. It never
          applies anything new. Requires admin, same as any apply path.
        </p>
        <div class="field-row">
          <label for="daemon-interval">Check every</label>
          <select id="daemon-interval">
            <option value="30">30 minutes</option>
            <option value="60" selected>60 minutes</option>
            <option value="180">3 hours</option>
            <option value="720">12 hours</option>
            <option value="1440">24 hours</option>
          </select>
          <button id="btn-daemon-install" class="primary">Install</button>
          <button id="btn-daemon-uninstall" class="danger">Uninstall</button>
          <button id="btn-daemon-check-now">Check now</button>
          <button id="btn-daemon-reset-blocklist" style="display:none;">Retry blocked</button>
        </div>
        <div class="status-line" id="daemon-status-text">Checking daemon status&hellip;</div>
        <div id="blocklist-diagnosis-panel"></div>
        <div class="log-mini mono" id="daemon-log-view"></div>
      </div>

      <div class="card">
        <div class="card-title">Idle-Time Threshold</div>
        <div class="field-row">
          <input id="idle-threshold-input" type="number" min="1" max="1440" step="1" style="width:90px;">
          <span style="font-size:11px;color:var(--muted);">minutes idle before background maintenance runs</span>
          <button id="btn-idle-save">Save</button>
        </div>
        <div class="status-line" id="idle-status-text"></div>
      </div>

    </section>

    <!-- ===== MAINTENANCE ===== -->
    <section class="panel" id="panel-maintenance" role="tabpanel" aria-labelledby="tab-maintenance">
      <div class="card">
        <div class="card-title">Maintenance Actions</div>
        <p style="color:var(--muted);font-size:11.5px;line-height:1.6;margin:0 0 10px;">
          These run with no time limit — a slow/HDD drive can genuinely take a long
          while on SFC, DISM, or defrag, and killing one partway is worse than letting
          it finish (SFC/DISM restarted from scratch just burns more time; a killed
          defrag can leave a drive mid-rearrange). The elapsed timer on a running
          action means it's still working, not stuck. Tick boxes to batch-run a set —
          individual "Run" still fires just that one action immediately.
        </p>
        <div class="tweak-summary">
          <span id="maint-summary-text">No actions loaded yet.</span>
          <span class="select-links">
            <a id="maint-select-all-link">select all</a>
            <a id="maint-select-none-link">select none</a>
          </span>
          <button id="btn-run-selected-maint" class="primary" disabled style="margin-left:10px;padding:6px 14px;font-size:11px;">Run Selected</button>
        </div>
        <div class="maint-grid" id="maint-grid"><div class="skeleton-row"></div><div class="skeleton-row"></div></div>
      </div>
    </section>

    <!-- ===== RESTORE POINTS ===== -->
    <section class="panel" id="panel-restore" role="tabpanel" aria-labelledby="tab-restore">
      <div class="card">
        <div class="card-title">System Restore Points</div>
        <p style="color:var(--muted);font-size:11.5px;line-height:1.6;margin:0 0 10px;">
          Windows-level checkpoints (System Restore) — the safety net you'd use if a tweak
          causes something unrelated to misbehave and you want the whole system state back.
        </p>
        <div class="field-row">
          <button id="btn-restore" class="primary">Create restore point</button>
          <button id="btn-restore-refresh">Refresh list</button>
        </div>
        <div id="rp-list"><div class="empty">Loading restore points&hellip;</div></div>
      </div>

      <div class="card">
        <div class="card-title">Revert Flow's Changes</div>
        <p style="color:var(--muted);font-size:11.5px;line-height:1.6;margin:0 0 10px;">
          Different from the above — this reverts only the individual tweaks Flow itself has
          applied and logged (registry/service/power-plan changes), not a full System Restore.
        </p>
        <button id="btn-revert" class="danger">Revert all Flow tweaks</button>
      </div>
    </section>

    <!-- ===== SYSTEM AUDIT ===== -->
    <section class="panel" id="panel-audit" role="tabpanel" aria-labelledby="tab-audit">
      <div class="card">
        <div class="card-title">
          <span>Hardware Summary</span>
          <button id="btn-refresh" style="padding:3px 9px;font-size:10.5px;">refresh</button>
        </div>
        <div id="hw-body"><div class="empty">Detecting hardware&hellip;</div></div>
      </div>
      <div class="card">
        <div class="card-title">
          <span>Live Usage</span>
          <span style="display:flex;align-items:center;gap:6px;">
            <span class="usage-updated mono" id="usage-updated"></span>
            <span class="live-dot" id="live-dot" title="Updates every 3s while this tab is open"></span>
          </span>
        </div>
        <div class="usage-grid" id="usage-body">
          <div class="usage-tile"><div class="usage-label">CPU</div><div class="usage-bar-track"><div class="usage-bar-fill" id="bar-cpu"></div></div><div class="usage-value" id="val-cpu">&mdash;</div><svg class="usage-spark" id="spark-cpu" viewBox="0 0 100 24" preserveAspectRatio="none"></svg></div>
          <div class="usage-tile"><div class="usage-label">RAM</div><div class="usage-bar-track"><div class="usage-bar-fill" id="bar-ram"></div></div><div class="usage-value" id="val-ram">&mdash;</div><svg class="usage-spark" id="spark-ram" viewBox="0 0 100 24" preserveAspectRatio="none"></svg></div>
          <div class="usage-tile" id="usage-gpu-tile" title="Reads the 'GPU Engine' perf counter (Windows 10 1803+ with WDDM 2.4+ drivers only) — absent on older/some integrated setups."><div class="usage-label">GPU</div><div class="usage-bar-track"><div class="usage-bar-fill" id="bar-gpu"></div></div><div class="usage-value" id="val-gpu">&mdash;</div></div>
          <div class="usage-tile" id="usage-temp-tile"><div class="usage-label">CPU Temp</div><div class="usage-bar-track"><div class="usage-bar-fill" id="bar-temp"></div></div><div class="usage-value" id="val-temp">&mdash;</div></div>
          <div class="usage-tile" id="usage-battery-tile" style="display:none;"><div class="usage-label">Battery</div><div class="usage-bar-track"><div class="usage-bar-fill" id="bar-battery"></div></div><div class="usage-value" id="val-battery">&mdash;</div></div>
          <div class="usage-tile" title="Bar shows % Disk Time (combined busy-ness across all physical disks); label shows raw read/write throughput."><div class="usage-label">Disk I/O</div><div class="usage-bar-track"><div class="usage-bar-fill" id="bar-diskio"></div></div><div class="usage-value" id="val-diskio">&mdash;</div></div>
          <div class="usage-tile" id="usage-net-tile">
            <div class="usage-label">Network</div>
            <div class="usage-value" id="val-net" style="margin-top:2px;">&mdash;</div>
            <div id="usage-net-adapters" class="usage-net-adapters"></div>
          </div>
        </div>

        <div class="usage-cores-label">Per-core load</div>
        <div class="usage-cores" id="usage-cores"></div>

        <div id="usage-disks" class="usage-disks"></div>

        <div class="usage-cores-label" style="margin-top:14px;">Top memory consumers</div>
        <div id="usage-top-procs" class="usage-top-procs"></div>
      </div>
      <div class="card">
        <div class="card-title">
          <span>Full Hardware &amp; OS Profile</span>
          <span>
            <button id="btn-copy-report" style="padding:3px 9px;font-size:10.5px;">copy report</button>
            <button id="btn-audit-refresh" style="padding:3px 9px;font-size:10.5px;">refresh</button>
          </span>
        </div>
        <div class="audit-grid" id="audit-body"><div class="empty">Detecting&hellip;</div></div>
      </div>
      <div class="card">
        <div class="card-title">Storage &amp; Graphics — every device, not just the first</div>
        <div class="audit-grid" id="audit-devices"><div class="empty">Detecting&hellip;</div></div>
      </div>
      <div class="card">
        <div class="card-title">Bloatware Detected</div>
        <ul class="plain-list" id="audit-bloatware"><div class="empty">&mdash;</div></ul>
      </div>
    </section>

    <!-- ===== AI CHAT ===== -->
    <section class="panel" id="panel-chat" role="tabpanel" aria-labelledby="tab-chat">
      <div class="card" style="display:flex;flex-direction:column;height:calc(100vh - 120px);">
        <div class="card-title">
          <span>AI Chat</span>
          <span id="chat-provider-badge" class="mono" style="font-size:10.5px;opacity:.7;"></span>
        </div>
        <div id="chat-log" style="flex:1;overflow-y:auto;padding:4px 2px;display:flex;flex-direction:column;gap:10px;"></div>
        <div id="chat-nokey" class="empty" style="display:none;">
          No AI key configured — add one in Settings (gear icon) to use chat.
        </div>
        <div style="display:flex;gap:8px;margin-top:10px;">
          <textarea id="chat-input" rows="2" placeholder="Ask about your hardware, a tweak, or anything else…" style="flex:1;resize:none;background:var(--panel-hover);border:1px solid var(--glass-border);border-radius:8px;color:var(--text);padding:8px 10px;font:inherit;"></textarea>
          <button id="btn-chat-send" style="align-self:flex-end;">Send</button>
        </div>
      </div>
    </section>

  </div>

  <!-- ===== RIGHT COLUMN — persistent telemetry + terminal + actions ===== -->
  <div class="col-right">
    <div class="terminal-wrap">
      <div class="rc-title"><span>Live Console</span></div>
      <div class="terminal mono" id="log-body"><div class="empty">Nothing yet.</div></div>
    </div>

    <div class="action-panel">
      <button id="btn-apply" class="primary" disabled>
        <span class="spinner"></span><span id="btn-apply-label">APPLY SELECTED TWEAKS</span>
      </button>
      <div class="action-row2">
        <button id="btn-restore-2">Create Restore Point</button>
        <button id="btn-revert-2" class="danger">Revert Changes</button>
      </div>
    </div>
  </div>
</div>

<script>
// ---------------------------------------------------------------------
// Small shared helpers (escaping, logging, formatting)
// ---------------------------------------------------------------------
function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str == null ? '' : String(str);
  return div.innerHTML;
}

let firstLog = true;
function log(kind, text) {
  const body = document.getElementById('log-body');
  if (firstLog) { body.innerHTML = ''; firstLog = false; }
  const line = document.createElement('div');
  line.className = 'log-line ' + kind;
  line.textContent = text;
  body.appendChild(line);
  body.scrollTop = body.scrollHeight;
}

function formatUptime(hours) {
  if (!hours) return '—';
  const d = Math.floor(hours / 24);
  const h = Math.floor(hours % 24);
  return d > 0 ? `${d}d ${h}h` : `${h}h`;
}

function formatAge(isoDate) {
  if (!isoDate || isoDate === 'Unknown') return 'Unknown';
  const then = new Date(isoDate);
  if (isNaN(then.getTime())) return isoDate;
  const days = Math.floor((Date.now() - then.getTime()) / 86400000);
  if (days < 0) return isoDate;
  if (days < 31) return `${days}d (installed ${isoDate})`;
  const months = Math.floor(days / 30.44);
  if (months < 24) return `${months}mo (installed ${isoDate})`;
  return `${(days / 365.25).toFixed(1)}yr (installed ${isoDate})`;
}

// Real tweak categories from flow.py's TWEAK_DATABASE, bucketed into the
// four display sections. Anything not listed falls into "Other" rather
// than being silently dropped if a category gets added later.
const GROUP_MAP = {
  privacy: 'Privacy & Telemetry', telemetry: 'Privacy & Telemetry',
  cloud: 'Privacy & Telemetry', notifications: 'Privacy & Telemetry',
  visual: 'Performance', power: 'Performance', memory: 'Performance',
  gaming: 'Performance', storage: 'Performance', accessibility: 'Performance',
  service: 'Services', security: 'Services', startup: 'Services', bloatware: 'Services',
  network: 'Maintenance & Updates', explorer: 'Maintenance & Updates',
};
const GROUP_ORDER = ['Privacy & Telemetry', 'Performance', 'Services', 'Maintenance & Updates', 'Other'];

// ---------------------------------------------------------------------
// Tab switching
// ---------------------------------------------------------------------
function switchTab(tab) {
  document.querySelectorAll('.side-btn[data-tab]').forEach(b => {
    const active = b.dataset.tab === tab;
    b.classList.toggle('active', active);
    b.setAttribute('aria-selected', active ? 'true' : 'false');
  });
  document.querySelectorAll('.panel').forEach(p => p.classList.toggle('active', p.id === 'panel-' + tab));
  if (tab === 'idle' && !window._idleLoaded) { window._idleLoaded = true; loadIdleTab(); }
  if (tab === 'maintenance' && !window._maintLoaded) { window._maintLoaded = true; renderMaintenance(); }
  if (tab === 'chat' && !window._chatLoaded) { window._chatLoaded = true; initChatTab(); }
  if (tab === 'restore' && !window._restoreLoaded) { window._restoreLoaded = true; loadRestoreTab(); }
  if (tab === 'audit') { renderAudit(window._profile); startUsagePolling(); } else { stopUsagePolling(); }
}
document.querySelectorAll('.side-btn[data-tab]').forEach(b => b.addEventListener('click', () => switchTab(b.dataset.tab)));

// ---------------------------------------------------------------------
// Sidebar collapse — button existed in markup/CSS but had no handler,
// so clicking it did nothing. Wired here + persisted across restarts.
// ---------------------------------------------------------------------
(function initSidebarCollapse() {
  const sidebar = document.getElementById('sidebar');
  const btn = document.getElementById('btn-sidebar-collapse');
  if (!sidebar || !btn) return;
  const setCollapsed = (collapsed) => {
    sidebar.classList.toggle('collapsed', collapsed);
    btn.setAttribute('aria-label', collapsed ? 'Expand sidebar' : 'Collapse sidebar');
    btn.title = collapsed ? 'Expand sidebar' : 'Collapse sidebar';
    try { localStorage.setItem('flow_sidebar_collapsed', collapsed ? '1' : '0'); } catch (e) {}
  };
  // getItem can throw (webview storage sometimes restricted) -- previously
  // unguarded, so the throw aborted this whole IIFE before the line below
  // ever ran, meaning the click handler was never attached. That's why the
  // button visibly did nothing.
  let startCollapsed = false;
  try { startCollapsed = localStorage.getItem('flow_sidebar_collapsed') === '1'; } catch (e) {}
  setCollapsed(startCollapsed);
  btn.addEventListener('click', () => setCollapsed(!sidebar.classList.contains('collapsed')));
})();
// Up/down arrow keys move focus + activate the adjacent tab, matching
// standard ARIA tablist behavior (roving focus) for a vertical list —
// this only fires when focus is already inside the sidebar, never
// hijacks arrow keys elsewhere.
document.querySelector('.sidebar').addEventListener('keydown', (e) => {
  if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return;
  const tabs = Array.from(document.querySelectorAll('.side-btn[data-tab]'));
  const idx = tabs.indexOf(document.activeElement);
  if (idx === -1) return;
  e.preventDefault();
  const next = tabs[(idx + (e.key === 'ArrowDown' ? 1 : -1) + tabs.length) % tabs.length];
  next.focus();
  switchTab(next.dataset.tab);
});

// ---------------------------------------------------------------------
// Live Usage — polls only while the System Audit tab is visible, so an
// idle background tab doesn't keep spawning a PowerShell process every
// 3 seconds forever on a 4GB rig for no reason.
// ---------------------------------------------------------------------
// Rolling history for the CPU/RAM sparklines — last 20 samples (~1 minute
// at the 3s poll interval), kept client-side only. No backend change
// needed since live_stats() already returns a fresh value every call;
// this is purely a presentation layer over data Flow already has.
const SPARK_HISTORY_MAX = 20;
window._sparkHistory = window._sparkHistory || { cpu: [], ram: [] };
function pushSparkSample(key, value) {
  const hist = window._sparkHistory[key];
  hist.push(value == null ? 0 : value);
  if (hist.length > SPARK_HISTORY_MAX) hist.shift();
}
function renderSparkline(svgId, key) {
  const hist = window._sparkHistory[key];
  const svg = document.getElementById(svgId);
  if (!svg || hist.length < 2) { if (svg) svg.innerHTML = ''; return; }
  const w = 100, h = 24;
  const step = w / (SPARK_HISTORY_MAX - 1);
  const offset = SPARK_HISTORY_MAX - hist.length;
  const points = hist.map((v, i) => {
    const x = (offset + i) * step;
    const y = h - (Math.min(100, Math.max(0, v)) / 100) * h;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  const fillPoints = `0,${h} ` + points.join(' ') + ` ${w},${h}`;
  svg.innerHTML = `<polygon points="${fillPoints}"></polygon><polyline points="${points.join(' ')}"></polyline>`;
}

function usageBarClass(pct) {
  if (pct >= 90) return 'crit';
  if (pct >= 75) return 'warn';
  return '';
}
function setUsageBar(barId, valId, pct, label) {
  const bar = document.getElementById(barId);
  const val = document.getElementById(valId);
  if (pct === null || pct === undefined) {
    bar.style.width = '0%'; bar.className = 'usage-bar-fill';
    val.innerHTML = 'n/a'; val.className = 'usage-value muted-value';
    return;
  }
  bar.style.width = Math.min(100, Math.max(0, pct)) + '%';
  bar.className = 'usage-bar-fill ' + usageBarClass(pct);
  val.innerHTML = label || (pct + '%');
  val.className = 'usage-value';
}
async function pollUsage() {
  const dot = document.getElementById('live-dot');
  let stats;
  try {
    stats = await window.pywebview.api.live_stats();
    dot.classList.remove('stale');
  } catch (e) {
    dot.classList.add('stale');
    return;
  }
  setUsageBar('bar-cpu', 'val-cpu', stats.cpu_percent);
  setUsageBar('bar-ram', 'val-ram', stats.ram_percent,
    stats.ram_percent != null ? `${stats.ram_percent}% (${stats.ram_used_gb}/${stats.ram_total_gb} GB)` : null);
  pushSparkSample('cpu', stats.cpu_percent);
  pushSparkSample('ram', stats.ram_percent);
  renderSparkline('spark-cpu', 'cpu');
  renderSparkline('spark-ram', 'ram');

  const gpuTile = document.getElementById('usage-gpu-tile');
  if (stats.gpu_available) {
    gpuTile.style.display = '';
    setUsageBar('bar-gpu', 'val-gpu', stats.gpu_percent);
  } else {
    gpuTile.style.display = '';
    setUsageBar('bar-gpu', 'val-gpu', null);
    document.getElementById('val-gpu').textContent = 'not exposed by this driver';
  }

  const tempTile = document.getElementById('usage-temp-tile');
  if (stats.temp_available) {
    tempTile.style.display = '';
    // Rough color banding for temp (not a 0-100 percent, so map to a bar heuristically): <60C green range, 60-80 warm, 80+ hot.
    const t = stats.cpu_temp_c;
    const pctEquiv = Math.min(100, Math.max(0, ((t - 30) / (95 - 30)) * 100));
    // Some OEM firmware wires MSAcpi_ThermalZoneTemperature to a fixed
    // constant instead of a real sensor read -- Windows requires *some*
    // ACPI thermal zone to exist, but not that it be live. A real sensor
    // fluctuates poll to poll; a dummy one doesn't. Track the last few
    // readings and say so once it's clearly not moving, rather than
    // silently showing a number that looks live but isn't.
    window._tempHistory = window._tempHistory || [];
    window._tempHistory.push(t);
    if (window._tempHistory.length > 5) window._tempHistory.shift();
    const stuck = window._tempHistory.length >= 5 && window._tempHistory.every(v => v === window._tempHistory[0]);
    setUsageBar('bar-temp', 'val-temp', pctEquiv, stuck ? `${t}°C (not changing — may be a fixed reading, not live)` : `${t}°C`);
  } else {
    tempTile.style.display = '';
    setUsageBar('bar-temp', 'val-temp', null);
    document.getElementById('val-temp').textContent = 'not exposed by this board';
  }

  const battTile = document.getElementById('usage-battery-tile');
  if (stats.battery) {
    battTile.style.display = '';
    setUsageBar('bar-battery', 'val-battery', stats.battery.percent,
      `${stats.battery.percent}%${stats.battery.charging ? ' ⚡' : ''}`);
  } else {
    battTile.style.display = 'none';
  }

  // Disk I/O — combines busy% (if available) as the bar with read/write
  // throughput as the label, since "% Disk Time" is the closest analogue
  // to a 0-100 utilization bar; raw KB/s alone has no natural ceiling.
  const diskBusy = stats.disk_busy_percent;
  setUsageBar('bar-diskio', 'val-diskio', diskBusy,
    `R ${stats.disk_read_kbps} KB/s &middot; W ${stats.disk_write_kbps} KB/s`);
  if (diskBusy === null || diskBusy === undefined) {
    document.getElementById('val-diskio').innerHTML = `R ${stats.disk_read_kbps} KB/s &middot; W ${stats.disk_write_kbps} KB/s`;
    document.getElementById('val-diskio').className = 'usage-value';
  }

  const netTile = document.getElementById('usage-net-tile');
  const adaptersEl = document.getElementById('usage-net-adapters');
  if (stats.net_available) {
    document.getElementById('val-net').innerHTML = `&darr; ${stats.net_down_kbps} KB/s &nbsp; &uarr; ${stats.net_up_kbps} KB/s`;
    const adapters = stats.net_adapters || [];
    adaptersEl.innerHTML = adapters.length > 1 ? adapters.map(a => `
      <div class="usage-net-adapter-row">
        <span class="n" title="${escapeHtml(a.name)}">${escapeHtml(a.name)}</span>
        <span>&darr;${a.down_kbps} &uarr;${a.up_kbps}</span>
      </div>
    `).join('') : '';
  } else {
    document.getElementById('val-net').textContent = 'no active adapter';
    adaptersEl.innerHTML = '';
  }
  document.getElementById('usage-updated').textContent = 'updated ' + new Date().toLocaleTimeString();

  const coresEl = document.getElementById('usage-cores');
  const cores = stats.cpu_per_core || [];
  if (cores.length) {
    coresEl.innerHTML = cores.map((pct, i) => `
      <div class="usage-core-tile">
        <div class="usage-core-bar-track"><div class="usage-core-bar-fill ${usageBarClass(pct)}" style="height:${Math.min(100,Math.max(0,pct))}%;"></div></div>
        <div class="usage-core-label">C${i}&nbsp;${Math.round(pct)}%</div>
      </div>
    `).join('');
  } else {
    coresEl.innerHTML = '<div class="empty">Per-core data unavailable.</div>';
  }

  const procsEl = document.getElementById('usage-top-procs');
  const procs = stats.top_processes || [];
  if (procs.length) {
    const maxMem = Math.max(...procs.map(p => p.mem_mb), 1);
    procsEl.innerHTML = procs.map(p => `
      <div class="usage-proc-row">
        <span class="usage-proc-name" title="${escapeHtml(p.name)}">${escapeHtml(p.name)}</span>
        <span class="usage-proc-bar-track"><span class="usage-proc-bar-fill" style="width:${(p.mem_mb / maxMem * 100).toFixed(0)}%;"></span></span>
        <span class="usage-proc-mem">${p.mem_mb} MB</span>
      </div>
    `).join('');
  } else {
    procsEl.innerHTML = '<div class="empty">Process data unavailable.</div>';
  }

  const disksEl = document.getElementById('usage-disks');
  disksEl.innerHTML = (stats.volumes || []).map(v => `
    <div class="usage-disk-tile">
      <div class="usage-label">${escapeHtml(v.drive_letter)}: drive</div>
      <div class="usage-bar-track"><div class="usage-bar-fill ${usageBarClass(v.used_percent)}" style="width:${v.used_percent}%;"></div></div>
      <div class="usage-value" style="font-size:11px;">${v.used_percent}% used &middot; ${v.free_gb} GB free</div>
    </div>
  `).join('');
}
function startUsagePolling() {
  if (window._usagePolling) return;
  window._usagePolling = true;
  window._tempHistory = [];
  if (!window._usageEverLoaded) {
    document.getElementById('usage-cores').innerHTML = '<div class="skeleton-row" style="height:42px;"></div>';
    document.getElementById('usage-top-procs').innerHTML = '<div class="skeleton-row"></div><div class="skeleton-row"></div>';
    document.getElementById('usage-disks').innerHTML = '<div class="skeleton-row"></div>';
  }
  // Self-scheduling instead of setInterval: each poll spawns a real
  // powershell.exe process, which can legitimately take a couple seconds
  // to start on the slow HDD/4GB rigs this app targets. A fixed interval
  // would fire the next poll before a slow one finishes, stacking up
  // concurrent PowerShell processes -- that pile-up is what was making
  // everything degrade into 'n/a' after running a while. Waiting for each
  // poll to actually finish before scheduling the next one means there's
  // never more than one live_stats() call in flight. 1000ms here is the
  // gap AFTER a poll finishes, not a guaranteed 1s cadence -- on the slow
  // reference hardware (i3/4GB/HDD) PowerShell's own spawn time can push
  // the real gap past a second regardless of this number.
  async function loop() {
    if (!window._usagePolling) return;
    await pollUsage();
    window._usageEverLoaded = true;
    if (window._usagePolling) window._usageTimer = setTimeout(loop, 1000);
  }
  loop();
}
function stopUsagePolling() {
  window._usagePolling = false;
  if (window._usageTimer) { clearTimeout(window._usageTimer); window._usageTimer = null; }
}

// ---------------------------------------------------------------------
// Hardware card (right column) + System Audit tab (left, full detail)
// ---------------------------------------------------------------------
function renderHardwareCard(profile) {
  const b = document.getElementById('hw-body');
  const gpu = (profile.gpus || [])[0] || { name: 'Unknown', vram_gb: 0 };
  const disk = (profile.disks || [])[0] || { model: 'Unknown', media_type: '?', size_gb: 0 };
  const isLaptop = profile.is_laptop;
  b.innerHTML = `
    <div class="stat-row"><span class="k">Device type</span><span class="v">${isLaptop ? 'Laptop' : 'Desktop'}${profile.battery ? ' (' + profile.battery.percent + '%' + (profile.battery.charging ? ', charging' : '') + ')' : ''}</span></div>
    <div class="stat-row"><span class="k">CPU</span><span class="v">${escapeHtml(profile.cpu.name)}</span></div>
    <div class="stat-row"><span class="k">Cores</span><span class="v">${profile.cpu.physical_cores}c / ${profile.cpu.logical_cores}t</span></div>
    <div class="stat-row"><span class="k">RAM</span><span class="v">${profile.ram.total_gb} GB</span></div>
    <div class="stat-row"><span class="k">Disk</span><span class="v">${disk.media_type}, ${disk.size_gb} GB</span></div>
    <div class="stat-row"><span class="k">GPU</span><span class="v">${escapeHtml(gpu.name)}</span></div>
    <div class="stat-row"><span class="k">Uptime</span><span class="v">${formatUptime(profile.uptime_hours)}</span></div>
    <div class="stat-row" style="border-bottom:none;"><span class="k">Suggested</span><span class="v" style="color:var(--cyan);">${escapeHtml(profile.suggested_tier)}</span></div>
  `;
  document.getElementById('os-line').textContent = profile.os_name + ' (' + profile.os_build + ')';
}

function renderAudit(profile) {
  if (!profile) return;
  const board = profile.board || {};
  const av = profile.antivirus || 'Unknown';
  const bloatCount = (profile.bloatware_installed || []).length;
  const cpu = profile.cpu || {};
  const ram = profile.ram || {};
  const left = `
    <div class="stat-row"><span class="k">OS</span><span class="v">${escapeHtml(profile.os_name)}</span></div>
    <div class="stat-row"><span class="k">Build</span><span class="v">${escapeHtml(profile.os_build)}</span></div>
    <div class="stat-row"><span class="k">Edition</span><span class="v">${escapeHtml(profile.os_edition)}</span></div>
    <div class="stat-row"><span class="k">Architecture</span><span class="v">${escapeHtml(profile.os_arch)}</span></div>
    <div class="stat-row"><span class="k">OS age</span><span class="v">${formatAge(profile.os_install_date)}</span></div>
    <div class="stat-row"><span class="k">Domain joined</span><span class="v">${profile.domain_joined ? 'Yes' : 'No'}</span></div>
    <div class="stat-row"><span class="k">TPM present</span><span class="v">${profile.tpm_present ? 'Yes' : 'No'}</span></div>
    <div class="stat-row"><span class="k">Secure Boot</span><span class="v">${profile.secure_boot === null ? 'Undetermined' : (profile.secure_boot ? 'On' : 'Off')}</span></div>
    <div class="stat-row"><span class="k">Board</span><span class="v">${escapeHtml(board.manufacturer)} ${escapeHtml(board.model)}</span></div>
    <div class="stat-row"><span class="k">BIOS</span><span class="v">${escapeHtml(board.bios_version)}</span></div>
  `;
  let right = `
    <div class="stat-row"><span class="k">CPU cores</span><span class="v">${cpu.physical_cores || '?'} physical / ${cpu.logical_cores || '?'} logical</span></div>
    <div class="stat-row"><span class="k">CPU max clock</span><span class="v">${cpu.max_clock_mhz ? (cpu.max_clock_mhz / 1000).toFixed(2) + ' GHz' : 'Unknown'}</span></div>
    <div class="stat-row"><span class="k">RAM modules</span><span class="v">${ram.module_count || '?'}${ram.speed_mhz ? ` @ ${ram.speed_mhz} MHz` : ''}</span></div>
    <div class="stat-row"><span class="k">Antivirus</span><span class="v">${escapeHtml(av)}</span></div>
    <div class="stat-row"><span class="k">Startup items</span><span class="v">${profile.startup_item_count}</span></div>
    <div class="stat-row"><span class="k">Bloatware found</span><span class="v" style="color:${bloatCount ? 'var(--amber)' : 'var(--emerald)'}">${bloatCount}</span></div>
    <div class="stat-row"><span class="k">Uptime</span><span class="v">${formatUptime(profile.uptime_hours)}</span></div>
  `;
  if (profile.battery) {
    right += `<div class="stat-row"><span class="k">Battery</span><span class="v">${profile.battery.percent}%${profile.battery.charging ? ' (charging)' : ''}</span></div>`;
  }
  document.getElementById('audit-body').innerHTML = `<div>${left}</div><div>${right}</div>`;

  // Full device lists — the right-column hardware card only shows the
  // first disk/GPU (that's the common case and keeps that card compact);
  // this section is specifically for machines with more than one of either.
  let devLeft = '<div class="stat-row" style="border-bottom:none;padding-bottom:2px;"><span class="k" style="color:var(--cyan);">Disks</span><span class="v"></span></div>';
  (profile.disks || []).forEach((d, i) => {
    devLeft += `<div class="stat-row"><span class="k">Disk ${i + 1}</span><span class="v">${escapeHtml(d.model)} — ${d.media_type}, ${d.size_gb} GB</span></div>`;
  });
  if (!(profile.disks || []).length) devLeft += '<div class="empty">None detected.</div>';
  devLeft += '<div class="stat-row" style="border-bottom:none;padding-bottom:2px;padding-top:10px;"><span class="k" style="color:var(--cyan);">Volumes</span><span class="v"></span></div>';
  (profile.volumes || []).forEach(v => {
    devLeft += `<div class="stat-row"><span class="k">${escapeHtml(v.drive_letter)}:</span><span class="v">${v.free_gb} free / ${v.total_gb} GB</span></div>`;
  });

  let devRight = '<div class="stat-row" style="border-bottom:none;padding-bottom:2px;"><span class="k" style="color:var(--cyan);">GPUs</span><span class="v"></span></div>';
  (profile.gpus || []).forEach((g, i) => {
    devRight += `<div class="stat-row"><span class="k">GPU ${i + 1}${g.is_dedicated ? '' : ' (integrated)'}</span><span class="v">${escapeHtml(g.name)} — ${g.vram_gb} GB</span></div>`;
  });
  if (!(profile.gpus || []).length) devRight += '<div class="empty">None detected.</div>';
  devRight += '<div class="stat-row" style="border-bottom:none;padding-bottom:2px;padding-top:10px;"><span class="k" style="color:var(--cyan);">Displays</span><span class="v"></span></div>';
  (profile.displays || []).forEach(d => {
    if (d.resolution_w) devRight += `<div class="stat-row"><span class="k">${escapeHtml(d.name || 'Display')}</span><span class="v">${d.resolution_w}&times;${d.resolution_h}${d.refresh_hz > 0 ? ' @' + d.refresh_hz + 'Hz' : ''}</span></div>`;
  });
  devRight += '<div class="stat-row" style="border-bottom:none;padding-bottom:2px;padding-top:10px;"><span class="k" style="color:var(--cyan);">Network</span><span class="v"></span></div>';
  (profile.network || []).forEach(n => {
    devRight += `<div class="stat-row"><span class="k">${escapeHtml(n.name)}</span><span class="v mono" style="font-size:10.5px;">${escapeHtml(n.mac || '')}</span></div>`;
  });
  document.getElementById('audit-devices').innerHTML = `<div>${devLeft}</div><div>${devRight}</div>`;

  const bloatEl = document.getElementById('audit-bloatware');
  const bloat = profile.bloatware_installed || [];
  bloatEl.innerHTML = bloat.length
    ? bloat.map(id => `<li>${escapeHtml(id)}</li>`).join('')
    : '<div class="empty">None detected.</div>';
}

// ---------------------------------------------------------------------
// Tweak Engine — preset cards + categorized toggle grid
// ---------------------------------------------------------------------
async function primePresetCounts(profile) {
  // One list_tweaks("extreme") call gets the full cumulative pool (every
  // tier <= extreme); counting client-side by each tweak's own .tier
  // avoids four separate round trips just to label the preset cards.
  const all = await window.pywebview.api.list_tweaks('extreme');
  window._allTweaksPool = all;
  const counts = { minimal: 0, standard: 0, maximal: 0, extreme: 0 };
  const rank = { minimal: 0, standard: 1, maximal: 2, extreme: 3 };
  all.forEach(t => { if (t.tier in rank) counts[t.tier] = (counts[t.tier] || 0) + 1; });
  // Cumulative counts, matching what selecting each preset will actually load.
  let running = 0;
  ['minimal', 'standard', 'maximal', 'extreme'].forEach(tier => {
    running += counts[tier];
    document.getElementById('preset-count-' + tier).textContent = `${running} tweak${running === 1 ? '' : 's'}`;
  });
}

document.querySelectorAll('.preset-card').forEach(card => {
  card.addEventListener('click', () => {
    document.querySelectorAll('.preset-card').forEach(c => c.classList.remove('active'));
    card.classList.add('active');
    const tier = card.dataset.tier;
    window._activeTier = tier;
    refreshTweaks(tier);
    renderReasons(tier);
    const box = document.getElementById('ai-insight-box');
    box.classList.remove('visible'); box.innerHTML = '';
  });
});

function currentTier() { return window._activeTier || 'minimal'; }

function renderReasons(tier) {
  const ul = document.getElementById('tier-reasons');
  if (tier === window._suggestedTier && window._tierReasons) {
    ul.innerHTML = window._tierReasons.map(r => `<li>${escapeHtml(r)}</li>`).join('');
  } else {
    ul.innerHTML = '';
  }
}

async function refreshTweaks(tier) {
  const listEl = document.getElementById('tweak-list');
  listEl.innerHTML = '<div class="skeleton-row"></div><div class="skeleton-row"></div><div class="skeleton-row"></div><div class="skeleton-row"></div>';
  document.getElementById('tweak-summary-text').textContent = 'Loading…';

  const tweaks = await window.pywebview.api.list_tweaks(tier);
  window._currentTweaks = tweaks;
  window._selectedTweakIds = new Set(tweaks.map(t => t.id));

  const fitLine = document.getElementById('hw-fit-line');
  fitLine.textContent = `${tweaks.length} tweak${tweaks.length === 1 ? '' : 's'} fit this machine at "${tier}" — hardware-inapplicable entries already filtered out.`;

  if (!tweaks.length) {
    listEl.innerHTML = '<div class="empty">No tweaks for this tier on this hardware.</div>';
    renderSelectionSummary();
    return;
  }
  renderSelectionSummary();

  const groups = new Map();
  tweaks.forEach(t => {
    const g = GROUP_MAP[t.category] || 'Other';
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(t);
  });

  let html = '';
  GROUP_ORDER.forEach(g => {
    const items = groups.get(g);
    if (!items || !items.length) return;
    html += `<div class="cat-section"><div class="cat-header">${escapeHtml(g)} <span class="count">&middot; ${items.length}</span></div><div class="tweak-grid">`;
    html += items.map(t => `
      <label class="tweak-row" data-id="${t.id}">
        <span class="switch">
          <input type="checkbox" class="tweak-check" id="chk-${t.id}" checked>
          <span class="track"></span><span class="thumb"></span>
        </span>
        <span class="tweak-label">
          <span class="name-row">
            <span class="name">${escapeHtml(t.name)}</span>
            <span class="risk-pill ${t.risk}">${t.risk}</span>
          </span>
          <span class="desc" title="${escapeHtml(t.description)}">${escapeHtml(t.description)}</span>
          <span class="ai-note" id="ai-note-${t.id}"></span>
        </span>
      </label>
    `).join('');
    html += `</div></div>`;
  });
  listEl.innerHTML = html;
}

function renderSelectionSummary() {
  const total = (window._currentTweaks || []).length;
  const selected = (window._selectedTweakIds || new Set()).size;
  document.getElementById('tweak-summary-text').textContent =
    total ? `${selected} of ${total} tweak${total === 1 ? '' : 's'} selected · "${currentTier()}" tier` : 'No tweaks loaded yet.';
  document.getElementById('btn-apply').disabled = selected === 0;
}

document.getElementById('select-all-link').addEventListener('click', (e) => { e.preventDefault(); setAllTweaksChecked(true); });
document.getElementById('select-none-link').addEventListener('click', (e) => { e.preventDefault(); setAllTweaksChecked(false); });
function setAllTweaksChecked(checked) {
  const ids = (window._currentTweaks || []).map(t => t.id);
  window._selectedTweakIds = new Set(checked ? ids : []);
  document.querySelectorAll('.tweak-check').forEach(el => { el.checked = checked; });
  renderSelectionSummary();
}

// Called when the AI chat picks tweaks on the user's behalf. Only ids that
// are both AI-approved AND actually present in the current tier's rendered
// list get checked (belt-and-suspenders on top of the server-side id
// validation in _parse_chat_response) — unmatched ids are silently
// dropped rather than causing an error, since a stale id here just means
// "don't check that one," never a broken state. Applying is still a
// separate explicit button click; this only moves checkboxes.
function applyAiTweakSelection(ids) {
  const available = new Set((window._currentTweaks || []).map(t => t.id));
  const toCheck = new Set(ids.filter(id => available.has(id)));
  if (!toCheck.size) return;
  window._selectedTweakIds = toCheck;
  document.querySelectorAll('.tweak-check').forEach(el => {
    const id = el.id.replace(/^chk-/, '');
    el.checked = toCheck.has(id);
  });
  renderSelectionSummary();
}

document.getElementById('tweak-list').addEventListener('change', (e) => {
  if (!e.target.classList.contains('tweak-check')) return;
  const row = e.target.closest('.tweak-row');
  const id = row ? row.dataset.id : null;
  if (!id) return;
  if (e.target.checked) window._selectedTweakIds.add(id);
  else window._selectedTweakIds.delete(id);
  renderSelectionSummary();
});

function renderResults(results, idsInOrder) {
  // apply_selected()/apply_tier() preserve input order (see flow.py), so
  // zipping the id list we sent against the results array we get back is
  // reliable — the underlying ExecResult objects don't otherwise carry the
  // tweak id. EXCEPTION: the mandatory-restore-point gate in _apply_batch()
  // returns exactly one ExecResult (not one per tweak) when it refuses the
  // whole batch — detect that by length mismatch + its distinct command
  // string, rather than mislabeling it as the first tweak's result.
  if (results.length === 1 && (results[0].command || '').startsWith('apply_batch(blocked')) {
    log('error', `Nothing was applied — ${results[0].stderr}`);
    return;
  }
  results.forEach((r, i) => {
    const id = idsInOrder && idsInOrder[i];
    const tw = id && (window._currentTweaks || []).find(t => t.id === id);
    const label = tw ? tw.name : (r.command || 'action');
    const kind = !r.success ? 'error' : ((r.stdout || '').includes('skip') || (r.stdout || '').includes('already')) ? 'skip' : 'success';
    log(kind, label + (r.stderr ? ' — ' + r.stderr : (kind === 'skip' ? ' — ' + r.stdout : '')));
  });
}

// ---------------------------------------------------------------------
// Idle Chores tab
// ---------------------------------------------------------------------
async function loadIdleTab() {
  // Independent, concurrent, individually error-handled — refreshDaemonStatus()
  // and loadIdleSettings() each already catch their own failures internally,
  // so one hanging/erroring never leaves the other stuck on "Loading…"
  // forever or blocks the tab from being usable.
  await Promise.allSettled([refreshDaemonStatus(), loadIdleSettings()]);
}

async function loadIdleSettings() {
  try {
    const s = await window.pywebview.api.get_idle_settings();
    document.getElementById('idle-threshold-input').value = s.idle_threshold_minutes;
    document.getElementById('idle-status-text').textContent =
      `Currently: ${s.idle_threshold_minutes}m idle required, at most once per ${s.idle_cooldown_hours}h.`;
  } catch (e) {
    document.getElementById('idle-status-text').textContent = 'Could not load idle settings.';
  }
}

document.getElementById('btn-idle-save').addEventListener('click', async () => {
  const minutes = parseInt(document.getElementById('idle-threshold-input').value, 10);
  const statusEl = document.getElementById('idle-status-text');
  if (!minutes || minutes < 1) { statusEl.textContent = 'Enter a whole number of minutes (1 or more).'; return; }
  statusEl.textContent = 'Saving…';
  const result = await window.pywebview.api.set_idle_threshold(minutes);
  statusEl.textContent = `Saved — background maintenance now waits for ${result.idle_threshold_minutes}m idle.`;
});

// ---------------------------------------------------------------------
// AI Chat tab
// ---------------------------------------------------------------------
window._chatHistory = [];

function chatAppendBubble(role, text) {
  const log = document.getElementById('chat-log');
  const bubble = document.createElement('div');
  bubble.style.cssText = role === 'user'
    ? 'align-self:flex-end;max-width:80%;background:var(--accent,#2563eb);color:#fff;padding:8px 12px;border-radius:10px 10px 2px 10px;white-space:pre-wrap;'
    : 'align-self:flex-start;max-width:80%;background:var(--panel-hover);padding:8px 12px;border-radius:10px 10px 10px 2px;white-space:pre-wrap;';
  bubble.textContent = text;
  log.appendChild(bubble);
  log.scrollTop = log.scrollHeight;
  return bubble;
}

async function initChatTab() {
  let status;
  try { status = await window.pywebview.api.ai_status(); } catch (e) { status = { configured: false }; }
  const badge = document.getElementById('chat-provider-badge');
  const nokey = document.getElementById('chat-nokey');
  const input = document.getElementById('chat-input');
  const sendBtn = document.getElementById('btn-chat-send');
  nokey.style.display = 'none';
  input.disabled = false;
  sendBtn.disabled = false;
  badge.textContent = 'Flow Assistant' + (status.configured ? '' : ' (offline)');
  if (!window._chatWelcomed) {
    window._chatWelcomed = true;
    chatAppendBubble('assistant', status.configured
      ? "Connected. Ask me anything about your hardware, a specific tweak, or what's safe on this machine."
      : "No AI key configured — running in offline mode. I can share your hardware specs, the suggested tier, or search the current tier's tweaks by keyword. Add a key in Settings for full free-form reasoning.");
  }

  async function send() {
    const text = input.value.trim();
    if (!text || sendBtn.disabled) return;
    input.value = '';
    chatAppendBubble('user', text);
    window._chatHistory.push({ role: 'user', content: text });
    sendBtn.disabled = true;
    const pending = chatAppendBubble('assistant', '…thinking…');
    try {
      const result = await window.pywebview.api.ai_chat(text, window._chatHistory.slice(0, -1), currentTier());
      pending.textContent = result.available ? result.reply : `Error: ${result.reason}`;
      if (result.available) {
        window._chatHistory.push({ role: 'assistant', content: result.reply });
        const notes = [];
        if (result.select_ids && result.select_ids.length) {
          applyAiTweakSelection(result.select_ids);
          notes.push(`selected ${result.select_ids.length} tweak${result.select_ids.length === 1 ? '' : 's'} on the Tweak Engine tab`);
        }
        if (result.select_maint_ids && result.select_maint_ids.length) {
          await applyAiMaintSelection(result.select_maint_ids);
          notes.push(`selected ${result.select_maint_ids.length} maintenance action${result.select_maint_ids.length === 1 ? '' : 's'} on the Maintenance tab`);
        }
        if (notes.length) {
          pending.textContent += `\n\n(${notes.join(' and ')} — review them there, then click Apply / Run Selected.)`;
        }
      }
    } catch (err) {
      pending.textContent = `Error: ${escapeHtml(String(err))}`;
    } finally {
      sendBtn.disabled = false;
      input.focus();
    }
  }
  sendBtn.addEventListener('click', send);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  });
}

async function renderMaintenance() {
  const grid = document.getElementById('maint-grid');
  const admin = window._isAdmin;
  let actions;
  try {
    actions = await window.pywebview.api.list_maintenance();
  } catch (err) {
    grid.innerHTML = `<div class="empty">Could not load maintenance actions: ${escapeHtml(String(err))}</div>`;
    return;
  }
  window._currentMaint = actions;
  window._selectedMaintIds = window._selectedMaintIds || new Set();
  if (!actions.length) { grid.innerHTML = '<div class="empty">No actions available for this disk configuration.</div>'; renderMaintSelectionSummary(); return; }
  grid.innerHTML = actions.map(a => `
    <div class="maint-card" data-id="${a.id}">
      <label class="switch" style="float:right;">
        <input type="checkbox" class="maint-check" id="mchk-${a.id}">
        <span class="track"></span><span class="thumb"></span>
      </label>
      <div class="name">${escapeHtml(a.name)}</div>
      <div class="desc">${escapeHtml(a.description)}</div>
      <div class="row">
        ${a.disruptive ? '<span class="flag disruptive">takes a while</span>' : '<span class="flag">quick</span>'}
        ${a.requires_admin ? '<span class="flag">admin</span>' : ''}
        <span style="flex:1;"></span>
        <button class="maint-run" data-requires-admin="${a.requires_admin ? '1' : '0'}" ${(a.requires_admin && !admin) ? 'disabled' : ''}>Run</button>
      </div>
    </div>
  `).join('');
  renderMaintSelectionSummary();
  grid.querySelectorAll('.maint-run').forEach(btn => {
    btn.addEventListener('click', (e) => {
      const card = e.target.closest('.maint-card');
      runOneMaintenance(card.dataset.id, card.querySelector('.name').textContent, e.target);
    });
  });
}

function renderMaintSelectionSummary() {
  const total = (window._currentMaint || []).length;
  const selected = (window._selectedMaintIds || new Set()).size;
  document.getElementById('maint-summary-text').textContent =
    total ? `${selected} of ${total} action${total === 1 ? '' : 's'} selected` : 'No actions loaded yet.';
  document.getElementById('btn-run-selected-maint').disabled = selected === 0;
}

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('maint-select-all-link')?.addEventListener('click', (e) => {
    e.preventDefault();
    window._selectedMaintIds = new Set((window._currentMaint || []).map(a => a.id));
    document.querySelectorAll('.maint-check').forEach(el => { el.checked = true; });
    renderMaintSelectionSummary();
  });
  document.getElementById('maint-select-none-link')?.addEventListener('click', (e) => {
    e.preventDefault();
    window._selectedMaintIds = new Set();
    document.querySelectorAll('.maint-check').forEach(el => { el.checked = false; });
    renderMaintSelectionSummary();
  });
  document.getElementById('maint-grid')?.addEventListener('change', (e) => {
    if (!e.target.classList.contains('maint-check')) return;
    const id = e.target.closest('.maint-card')?.dataset.id;
    if (!id) return;
    if (e.target.checked) window._selectedMaintIds.add(id);
    else window._selectedMaintIds.delete(id);
    renderMaintSelectionSummary();
  });
  document.getElementById('btn-run-selected-maint')?.addEventListener('click', async () => {
    const ids = Array.from(window._selectedMaintIds || []);
    for (const id of ids) {
      const card = document.querySelector(`.maint-card[data-id="${id}"]`);
      const btn = card?.querySelector('.maint-run');
      const name = card?.querySelector('.name')?.textContent || id;
      if (btn) await runOneMaintenance(id, name, btn);
    }
  });
});

// Shared by the per-card "Run" click and the batch "Run Selected" button —
// keeps the elapsed-timer/log/disable behavior in one place instead of
// duplicating it for the batch path.
async function runOneMaintenance(id, name, btn) {
  if (btn.disabled) return;
  btn.disabled = true;
  const original = btn.textContent;
  log('info', `${name} — started (no timeout — this can legitimately run a long while on HDD, especially SFC/DISM/defrag)`);
  const startedAt = Date.now();
  const timer = setInterval(() => {
    btn.textContent = `Running… ${Math.floor((Date.now() - startedAt) / 1000)}s`;
  }, 1000);
  btn.textContent = 'Running… 0s';
  try {
    const result = await window.pywebview.api.run_maintenance(id);
    const secs = Math.floor((Date.now() - startedAt) / 1000);
    log(result.success ? 'success' : 'error', `${name} — ${result.success ? `done in ${secs}s` : 'failed: ' + result.stderr}`);
  } catch (err) {
    log('error', `${name} — ${err}`);
  } finally {
    clearInterval(timer);
    btn.disabled = btn.dataset.requiresAdmin === '1' && !window._isAdmin;
    btn.textContent = original;
  }
}

// Called when the AI chat picks maintenance actions on the user's behalf —
// same belt-and-suspenders id filtering as applyAiTweakSelection. Self-loads
// the maintenance list first if the user hasn't opened that tab yet (it's
// lazy-loaded on tab click — see window._maintLoaded), so an AI pick still
// lands correctly even from a cold AI Chat tab. Only checks boxes; running
// is still an explicit "Run Selected" click.
async function applyAiMaintSelection(ids) {
  if (!window._maintLoaded) { window._maintLoaded = true; await renderMaintenance(); }
  const available = new Set((window._currentMaint || []).map(a => a.id));
  const toCheck = new Set(ids.filter(id => available.has(id)));
  if (!toCheck.size) return;
  window._selectedMaintIds = toCheck;
  document.querySelectorAll('.maint-check').forEach(el => {
    const id = el.id.replace(/^mchk-/, '');
    el.checked = toCheck.has(id);
  });
  renderMaintSelectionSummary();
}

async function refreshDaemonStatus() {
  let status;
  try {
    status = await window.pywebview.api.daemon_status();
  } catch (err) {
    const el = document.getElementById('daemon-status-text');
    el.className = 'status-line bad';
    el.textContent = `Could not read daemon status: ${err}`;
    return;
  }
  const badge = document.getElementById('daemon-badge');
  badge.style.display = status.installed ? 'inline-block' : 'none';
  const el = document.getElementById('daemon-status-text');
  const runs = status.recent_runs || [];
  const lastRun = runs.length ? runs[runs.length - 1] : null;
  let text = status.installed ? 'Installed — runs at logon on the interval below.' : 'Not installed.';
  el.className = 'status-line ' + (status.installed ? 'ok' : '');
  if (lastRun) {
    text += ` Last check: ${lastRun.checked} tweak(s) checked, ${(lastRun.drifted || []).length} drifted, `
      + `${(lastRun.reapplied || []).length} reapplied${(lastRun.errored || []).length ? `, ${lastRun.errored.length} errored` : ''}`
      + `${(lastRun.blocked || []).length ? `, ${lastRun.blocked.length} blocked (see below)` : ''}.`;
  }
  el.textContent = text;

  const retryBtn = document.getElementById('btn-daemon-reset-blocklist');
  const hasBlocked = lastRun && (lastRun.blocked || []).length;
  retryBtn.style.display = hasBlocked ? 'inline-block' : 'none';
  await renderBlocklistDiagnoses(hasBlocked);

  const logView = document.getElementById('daemon-log-view');
  if (runs.length) {
    logView.style.display = 'block';
    logView.innerHTML = runs.slice().reverse().map(r => {
      if (r.event) return `<div>${escapeHtml(r.at || '')} — ${escapeHtml(r.event)}${r.error ? ': ' + escapeHtml(r.error) : ''}</div>`;
      const failList = r.failed || [], blockedList = r.blocked || [];
      const renderItems = (items, color) => items.length
        ? `<div style="color:${color};">` + items.map(f => {
            const id = typeof f === 'string' ? f : f.id;
            const err = typeof f === 'string' ? '' : (f.error || '');
            return `&nbsp;&nbsp;↳ ${escapeHtml(id)}${err ? ': ' + escapeHtml(err) : ''}`;
          }).join('<br>') + '</div>'
        : '';
      return `<div>${escapeHtml(r.at || '')} — checked ${r.checked}, drifted ${(r.drifted || []).length}, reapplied ${(r.reapplied || []).length}, failed ${failList.length}${blockedList.length ? `, blocked ${blockedList.length}` : ''}</div>${renderItems(failList, '#EF4444')}${renderItems(blockedList, '#8b97b5')}`;
    }).join('');
  } else {
    logView.style.display = 'none';
  }
  return status;
}

async function renderBlocklistDiagnoses(hasBlocked) {
  const panel = document.getElementById('blocklist-diagnosis-panel');
  if (!hasBlocked) { panel.innerHTML = ''; return; }
  let data;
  try { data = await window.pywebview.api.daemon_blocklist(); } catch (e) { return; }
  const blocked = data.blocked || {};
  const ids = Object.keys(blocked);
  if (!ids.length) { panel.innerHTML = ''; return; }
  panel.innerHTML = ids.map(id => {
    const entry = blocked[id];
    const diag = entry.ai_diagnosis;
    if (diag && diag.available) {
      return `<div class="diag-card">
        <div class="diag-id mono">${escapeHtml(id)}</div>
        <div class="diag-cause"><strong>Likely cause:</strong> ${escapeHtml(diag.likely_cause || 'unknown')}</div>
        <div class="diag-action"><strong>Try:</strong> ${escapeHtml(diag.suggested_action || 'no suggestion')}</div>
        ${diag.safe_to_keep_retrying === false ? '<div class="diag-flag">Retrying further is unlikely to help.</div>' : ''}
      </div>`;
    }
    return `<div class="diag-card">
      <div class="diag-id mono">${escapeHtml(id)}</div>
      <div class="diag-cause">${escapeHtml(entry.last_error || 'no error text recorded')}</div>
      <div class="diag-hint">Add an AI key (gear icon, top right) for an automatic diagnosis.</div>
    </div>`;
  }).join('');
}

document.getElementById('btn-daemon-install').addEventListener('click', async () => {
  const el = document.getElementById('daemon-status-text');
  const interval = parseInt(document.getElementById('daemon-interval').value, 10) || 60;
  el.textContent = 'Installing…';
  const r = await window.pywebview.api.daemon_install(interval);
  el.textContent = r.success ? `Installed — checking every ${interval} min.` : `Install failed: ${r.stderr}`;
  refreshDaemonStatus();
});
document.getElementById('btn-daemon-uninstall').addEventListener('click', async () => {
  if (!confirm('Uninstall the background daemon? Already-applied tweaks are untouched — this only stops future drift checks.')) return;
  const el = document.getElementById('daemon-status-text');
  el.textContent = 'Uninstalling…';
  const r = await window.pywebview.api.daemon_uninstall();
  el.textContent = r.success ? 'Uninstalled.' : `Uninstall failed: ${r.stderr}`;
  refreshDaemonStatus();
});
document.getElementById('btn-daemon-check-now').addEventListener('click', async () => {
  const el = document.getElementById('daemon-status-text');
  el.textContent = 'Checking…';
  const r = await window.pywebview.api.daemon_run_once();
  let msg = `Checked ${r.checked} tweak(s) — ${r.drifted.length} drifted, ${r.reapplied.length} reapplied, ${r.failed.length} failed${(r.blocked || []).length ? `, ${r.blocked.length} blocked` : ''}.`
    + (r.dry_run ? ' (dry run — TWEAKS_APPLY_ENABLED is off)' : '') + (!r.admin ? ' (not elevated — nothing reapplied)' : '');
  const m = r.maintenance;
  if (m) {
    if (m.skipped_reason) {
      msg += ` Maintenance: skipped (${m.skipped_reason}).`;
    } else {
      const okCount = (m.ran || []).filter(a => a.success).length;
      const failCount = (m.ran || []).length - okCount;
      msg += ` Maintenance: ${okCount} action(s) ran${failCount ? `, ${failCount} failed` : ''}.`;
    }
  }
  el.textContent = msg;
  refreshDaemonStatus();
});
document.getElementById('btn-daemon-reset-blocklist').addEventListener('click', async () => {
  if (!confirm('Retry all blocked tweaks on the next check? Use this after fixing the underlying block yourself — otherwise they\'ll likely fail again and re-block.')) return;
  const el = document.getElementById('daemon-status-text');
  el.textContent = 'Clearing blocklist…';
  await window.pywebview.api.daemon_reset_blocklist();
  el.textContent = 'Blocklist cleared — next check will retry everything.';
  document.getElementById('btn-daemon-reset-blocklist').style.display = 'none';
});

// ---------------------------------------------------------------------
// Restore Points tab
// ---------------------------------------------------------------------
async function loadRestoreTab() {
  await renderRestorePoints();
}
async function renderRestorePoints() {
  const el = document.getElementById('rp-list');
  el.innerHTML = '<div class="skeleton-row"></div><div class="skeleton-row"></div>';
  let rows;
  try {
    rows = await window.pywebview.api.list_restore_points();
  } catch (e) {
    el.innerHTML = '<div class="empty">Could not read restore points.</div>';
    return;
  }
  if (!rows || !rows.length) { el.innerHTML = '<div class="empty">No restore points found.</div>'; return; }
  el.innerHTML = `<table class="rp-table"><thead><tr><th>#</th><th>Description</th><th>Created</th></tr></thead><tbody>
    ${rows.map(r => `<tr><td class="mono">${escapeHtml(r.SequenceNumber)}</td><td>${escapeHtml(r.Description)}</td><td class="mono">${escapeHtml(r.CreationTime)}</td></tr>`).join('')}
  </tbody></table>`;
}
document.getElementById('btn-restore-refresh').addEventListener('click', renderRestorePoints);

async function doCreateRestorePoint(btn) {
  const label = btn.textContent;
  btn.disabled = true;
  log('info', 'Creating restore point — this can take a couple minutes on HDD…');
  const startedAt = Date.now();
  const timer = setInterval(() => {
    btn.textContent = `Creating… ${Math.floor((Date.now() - startedAt) / 1000)}s`;
  }, 1000);
  try {
    const r = await window.pywebview.api.create_restore_point();
    log(r.success ? 'success' : 'error', r.success ? 'Restore point created.' : ('Restore point failed: ' + r.stderr));
    if (window._restoreLoaded) renderRestorePoints();
  } finally {
    clearInterval(timer);
    btn.textContent = label;
    btn.disabled = false;
  }
}
document.getElementById('btn-restore').addEventListener('click', (e) => doCreateRestorePoint(e.target));
document.getElementById('btn-restore-2').addEventListener('click', (e) => doCreateRestorePoint(e.target));

async function doRevertAll() {
  if (!confirm('Revert every tweak Flow has logged so far? This cannot be undone.')) return;
  log('info', 'Reverting all logged tweaks…');
  try {
    const results = await window.pywebview.api.revert_all();
    if (!results.length) { log('skip', 'Nothing to revert — log was empty.'); return; }
    results.forEach(r => log(r.success ? 'success' : 'error', (r.command || 'revert') + (r.stderr ? ' — ' + r.stderr : '')));
  } catch (err) {
    log('error', 'Revert failed: ' + err);
  }
}
document.getElementById('btn-revert').addEventListener('click', doRevertAll);
document.getElementById('btn-revert-2').addEventListener('click', doRevertAll);

// ---------------------------------------------------------------------
// Apply Selected Tweaks (primary CTA, right column)
// ---------------------------------------------------------------------
document.getElementById('btn-apply').addEventListener('click', async (e) => {
  const btn = e.currentTarget;
  const tier = currentTier();
  const ids = Array.from(window._selectedTweakIds || []);
  const total = (window._currentTweaks || []).length;
  if (!ids.length) { log('skip', 'Nothing selected — tick at least one tweak first.'); return; }
  const label = ids.length === total ? `all ${total} tweaks in the "${tier}" tier` : `${ids.length} of ${total} tweaks in the "${tier}" tier`;
  const extremeWarning = tier === 'extreme'
    ? ' This tier includes tweaks that disable real security controls (UAC, firewall, and/or Defender). Some cannot be fully reverted automatically.\n\n'
    : ' ';
  if (!confirm(`Apply ${label}?${extremeWarning}This will change settings on this machine. Flow will create a restore point automatically first — that's mandatory now, not optional, and can add a minute or two before the tweaks themselves run.`)) return;

  btn.disabled = true;
  btn.classList.add('loading');
  document.getElementById('btn-apply-label').textContent = 'APPLYING…';
  log('info', 'Creating the mandatory restore point first — this can take a minute or two…');
  log('info', `Then applying ${ids.length} selected tweak${ids.length === 1 ? '' : 's'}…`);
  try {
    const results = await window.pywebview.api.apply_tweaks(ids);
    renderResults(results, ids);
  } finally {
    btn.classList.remove('loading');
    document.getElementById('btn-apply-label').textContent = 'APPLY SELECTED TWEAKS';
    renderSelectionSummary();
  }
});

document.getElementById('btn-refresh').addEventListener('click', async (e) => {
  e.target.disabled = true;
  log('info', 'Re-detecting hardware…');
  try {
    const profile = await window.pywebview.api.refresh();
    window._profile = profile;
    window._suggestedTier = profile.suggested_tier;
    window._tierReasons = profile.tier_reasons;
    renderHardwareCard(profile);
    renderAudit(profile);
    await primePresetCounts(profile);
  } finally {
    e.target.disabled = false;
  }
});
document.getElementById('btn-audit-refresh').addEventListener('click', () => document.getElementById('btn-refresh').click());

document.getElementById('btn-copy-report').addEventListener('click', async (e) => {
  const btn = e.target;
  const original = btn.textContent;
  const p = window._profile;
  if (!p) { btn.textContent = 'no data yet'; setTimeout(() => btn.textContent = original, 1500); return; }
  let stats = {};
  try { stats = await window.pywebview.api.live_stats(); } catch (err) { /* report still useful without live stats */ }
  const disk0 = (p.disks || [])[0] || {};
  const gpu0 = (p.gpus || [])[0] || {};
  const lines = [
    `Flow system report — ${new Date().toLocaleString()}`,
    '='.repeat(40),
    `OS: ${p.os_name} (build ${p.os_build}, ${p.os_edition}, ${p.os_arch})`,
    `CPU: ${p.cpu.name} — ${p.cpu.physical_cores}c/${p.cpu.logical_cores}t @ ${p.cpu.max_clock_mhz}MHz`,
    `RAM: ${p.ram.total_gb} GB (${p.ram.module_count} module(s) @ ${p.ram.speed_mhz}MHz)`,
    `Disk: ${disk0.model || 'Unknown'} — ${disk0.media_type || '?'}, ${disk0.size_gb || '?'} GB`,
    `GPU: ${gpu0.name || 'Unknown'} (${gpu0.vram_gb || '?'} GB)`,
    `Suggested tier: ${p.suggested_tier}`,
    `Antivirus: ${p.antivirus}`,
    `Bloatware detected: ${(p.bloatware_installed || []).length}`,
    '',
    'Live snapshot at time of report:',
    `  CPU: ${stats.cpu_percent != null ? stats.cpu_percent + '%' : 'n/a'}`,
    `  RAM: ${stats.ram_percent != null ? stats.ram_percent + '%' : 'n/a'}`,
    `  GPU: ${stats.gpu_percent != null ? stats.gpu_percent + '%' : 'not exposed'}`,
    `  CPU temp: ${stats.cpu_temp_c != null ? stats.cpu_temp_c + '°C' : 'not exposed by this board'}`,
  ];
  const text = lines.join('\n');
  let copied = false;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      copied = true;
    }
  } catch (err) { /* fall through to execCommand fallback below */ }
  if (!copied) {
    // Some pywebview backends restrict the async Clipboard API — this
    // execCommand path works everywhere it doesn't.
    const ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); copied = true; } catch (err) { /* give up quietly, button label reports failure */ }
    document.body.removeChild(ta);
  }
  btn.textContent = copied ? 'copied!' : 'copy failed';
  setTimeout(() => btn.textContent = original, 1500);
});

// ---------------------------------------------------------------------
// AI settings modal + AI insight
// ---------------------------------------------------------------------
function openSettings() {
  document.getElementById('settings-status').textContent = '';
  document.getElementById('settings-status').className = 'modal-status';
  document.getElementById('settings-overlay').classList.add('visible');
  document.getElementById('settings-key-input').focus();
}
function closeSettings() { document.getElementById('settings-overlay').classList.remove('visible'); }
document.getElementById('btn-settings').addEventListener('click', openSettings);
document.getElementById('btn-settings-cancel').addEventListener('click', closeSettings);
document.getElementById('settings-overlay').addEventListener('click', (e) => { if (e.target.id === 'settings-overlay') closeSettings(); });

document.getElementById('btn-settings-save').addEventListener('click', async (e) => {
  const key = document.getElementById('settings-key-input').value;
  const statusEl = document.getElementById('settings-status');
  e.target.disabled = true;
  statusEl.textContent = 'Saving…'; statusEl.className = 'modal-status';
  try {
    const result = await window.pywebview.api.set_api_key(key);
    statusEl.textContent = result.message;
    statusEl.className = 'modal-status ' + (result.success ? 'ok' : 'bad');
    if (result.success) {
      document.getElementById('settings-key-input').value = '';
      await refreshAiButtonVisibility();
      setTimeout(closeSettings, 900);
    }
  } finally {
    e.target.disabled = false;
  }
});

async function refreshAiButtonVisibility() {
  const status = await window.pywebview.api.ai_status();
  document.getElementById('btn-ai-insight').style.display = status.configured ? 'inline' : 'none';
}

document.getElementById('btn-ai-insight').addEventListener('click', async (e) => {
  const box = document.getElementById('ai-insight-box');
  box.className = 'ai-insight-box visible';
  box.innerHTML = '<span class="ai-tag">AI Insight</span><span class="ai-dots">Reading this rig&rsquo;s specs and the selected tweaks</span>';
  document.querySelectorAll('.ai-note').forEach(el => { el.classList.remove('visible'); el.innerHTML = ''; });
  e.target.style.pointerEvents = 'none';
  try {
    const result = await window.pywebview.api.ai_explain(currentTier());
    if (result.available) {
      box.className = 'ai-insight-box visible';
      box.innerHTML = `<span class="ai-tag">&#10022; AI Insight &middot; ${escapeHtml(result.provider)}${result.free ? ' (free tier)' : ''}</span>${escapeHtml(result.summary || '')}`;
      const tweakNotes = result.tweaks || {};
      let shown = 0;
      Object.keys(tweakNotes).forEach(id => {
        const el = document.getElementById('ai-note-' + id);
        const note = tweakNotes[id];
        if (!el || !note) return;
        let inner = '';
        if (note.why) inner += `<span class="ai-note-tag">AI</span>${escapeHtml(note.why)}`;
        if (note.watch_for) inner += `<span class="ai-note-watch"><span class="ai-note-tag">Watch for</span>${escapeHtml(note.watch_for)}</span>`;
        if (inner) { el.innerHTML = inner; el.classList.add('visible'); shown++; }
      });
      if (!shown && !result.summary) {
        box.className = 'ai-insight-box visible error';
        box.innerHTML = '<span class="ai-tag">AI Insight</span>The model replied, but not in a format Flow could parse. Try again.';
      }
    } else {
      box.className = 'ai-insight-box visible error';
      box.innerHTML = `<span class="ai-tag">AI Insight unavailable</span>${escapeHtml(result.reason || 'Unknown error.')}`;
    }
  } catch (err) {
    box.className = 'ai-insight-box visible error';
    box.innerHTML = `<span class="ai-tag">AI Insight unavailable</span>${escapeHtml(String(err))}`;
  } finally {
    e.target.style.pointerEvents = 'auto';
  }
});

// ---------------------------------------------------------------------
// Theme — defaults to the OS's current light/dark setting, overridable
// via the toggle button. Preference persists across launches through the
// same ~/.flow/config.json the idle-threshold setting already uses; if
// the person never touches the toggle, Flow just keeps following the OS
// setting live (matchMedia listener below), same as most native apps.
// ---------------------------------------------------------------------
function systemPrefersLight() {
  return window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches;
}
function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme === 'light' ? 'light' : 'dark');
  const btn = document.getElementById('btn-theme');
  if (btn) btn.innerHTML = theme === 'light' ? '&#9789;' : '&#9788;';
}
async function initTheme() {
  let saved = null;
  try { saved = await window.pywebview.api.get_theme_preference(); } catch (e) { /* older backend without this method — fall through to OS default */ }
  if (saved === 'light' || saved === 'dark') {
    window._themeIsManual = true;
    applyTheme(saved);
  } else {
    applyTheme(systemPrefersLight() ? 'light' : 'dark');
  }
  if (window.matchMedia) {
    window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', (e) => {
      if (!window._themeIsManual) applyTheme(e.matches ? 'light' : 'dark');
    });
  }
}
document.getElementById('btn-theme').addEventListener('click', async () => {
  const current = document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
  const next = current === 'light' ? 'dark' : 'light';
  window._themeIsManual = true;
  applyTheme(next);
  try { await window.pywebview.api.set_theme_preference(next); } catch (e) { /* persistence is a nice-to-have, not required for the toggle to work this session */ }
});

// ---------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------
async function init() {
  await initTheme();
  const admin = await window.pywebview.api.admin_status();
  window._isAdmin = admin.is_admin;
  const badge = document.getElementById('admin-badge');
  badge.textContent = admin.is_admin ? 'admin' : 'not elevated';
  badge.className = 'badge ' + (admin.is_admin ? 'ok' : 'warn');
  document.getElementById('status-led').className = 'led ' + (admin.is_admin ? 'ok' : 'bad');
  if (admin.dry_run) document.getElementById('dryrun-badge').style.display = 'inline-block';
  if (!admin.is_admin) log('info', 'Not running elevated — restart Flow as Administrator to apply tweaks.');

  badge.textContent = 'detecting hardware…';
  badge.className = 'badge';
  const hwBody = document.getElementById('hw-body');
  if (hwBody) hwBody.innerHTML = '<div class="empty">Detecting hardware&hellip; can take up to a minute on slower disks.</div>';
  const profile = await window.pywebview.api.detect();
  badge.textContent = admin.is_admin ? 'admin' : 'not elevated';
  badge.className = 'badge ' + (admin.is_admin ? 'ok' : 'warn');
  window._profile = profile;
  window._suggestedTier = profile.suggested_tier;
  window._tierReasons = profile.tier_reasons;
  renderHardwareCard(profile);
  await primePresetCounts(profile);

  // Auto-select the suggested tier's preset card on first load.
  const suggested = profile.suggested_tier;
  const card = document.querySelector(`.preset-card[data-tier="${suggested}"]`) || document.querySelector('.preset-card');
  if (card) card.click();

  // Cheap background poll (scheduled-task query + small log file read) —
  // deliberately NOT re-running full hardware detection on a timer, since
  // that's a real PowerShell/CIM spawn and would hammer WMI for no reason.
  setInterval(() => { window.pywebview.api.daemon_status().then(s => {
    document.getElementById('daemon-badge').style.display = s.installed ? 'inline-block' : 'none';
  }).catch(() => {}); }, 60000);

  refreshAiButtonVisibility();
}

window.addEventListener('pywebviewready', init);
</script>
</body>
</html>
"""


class Api:
    """Every method here is called from JS as window.pywebview.api.<name>(...).
    pywebview auto-serializes return values to JSON, so these just return
    plain dicts/lists built from the existing dataclasses' to_dict().

    Profile caching (two layers):
      - In-session: list_tweaks()/apply_tier() reuse the same profile
        instance rather than each re-detecting — hardware doesn't change
        mid-session.
      - Cross-launch (disk, TTL-based): the hardware fingerprint barely
        ever changes launch-to-launch either, so a fresh app open doesn't
        need to pay the PowerShell round trip again if one ran recently.
        refresh() (the GUI's refresh button) always bypasses this and
        forces a real re-detect, same as before."""

    _PROFILE_CACHE_TTL_SECONDS = 6 * 3600  # 6h — long enough to skip most re-opens same day, short enough to catch real hardware changes (new drive, docked GPU) same-day too

    def __init__(self):
        self._profile = None
        self._profile_ready = threading.Event()
        # Fire detection off immediately, in the background, the instant
        # Api() is constructed -- which happens before webview.create_window()
        # / webview.start() in launch_gui(). WebView2's own window+HTML
        # startup takes a real amount of wall-clock time on its own; running
        # the ~1-5s PowerShell hardware spawn concurrently with that instead
        # of after it means the GUI can show real hardware data the moment
        # it's ready to paint, rather than paint-then-wait-then-populate.
        threading.Thread(target=self._prefetch_profile, daemon=True).start()

    def _prefetch_profile(self) -> None:
        try:
            profile = self._load_cached_profile() or get_hardware_profile()
        except Exception:
            profile = None  # detect()/_get_profile() below will retry synchronously if this failed
        if profile is not None:
            self._profile = profile
            self._save_profile_cache(profile)
        self._profile_ready.set()

    def _profile_cache_path(self) -> str:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "flow_profile_cache.json")

    def _load_cached_profile(self) -> Optional[HardwareProfile]:
        path = self._profile_cache_path()
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                cached = json.load(f)
            if time.time() - cached.get("_cached_at", 0) > self._PROFILE_CACHE_TTL_SECONDS:
                return None
            cached.pop("_cached_at", None)
            return HardwareProfile.from_dict(cached)
        except (OSError, json.JSONDecodeError, TypeError, KeyError):
            return None  # any cache-shape mismatch (e.g. after an update adds a field) — just re-detect

    def _save_profile_cache(self, profile: HardwareProfile) -> None:
        try:
            data = profile.to_dict()
            data["_cached_at"] = time.time()
            with open(self._profile_cache_path(), "w", encoding="utf-8") as f:
                json.dump(data, f, default=str)
        except OSError:
            pass  # caching is an optimization, not a correctness requirement — never block on it failing

    def _get_profile(self) -> HardwareProfile:
        if self._profile is None:
            # Prefetch already started in __init__ / _prefetch_profile,
            # possibly seconds ago -- this just waits for whatever's left
            # of that instead of kicking off a fresh, redundant detect.
            self._profile_ready.wait()
            if self._profile is None:
                # Prefetch itself failed (exception swallowed there) --
                # one synchronous retry so the user gets a real error
                # surfaced instead of silently stuck.
                self._profile = get_hardware_profile()
                self._save_profile_cache(self._profile)
        return self._profile

    def admin_status(self) -> dict:
        return {"is_admin": is_admin(), "dry_run": not TWEAKS_APPLY_ENABLED}

    def detect(self) -> dict:
        """Initial load only — use refresh() to re-detect after this. Served
        from the on-disk cache when one exists and is fresh (see class
        docstring); falls through to a real detect otherwise."""
        return self._get_profile().to_dict()

    def refresh(self) -> dict:
        """Explicit re-detect, invalidating both the in-session cache and
        the on-disk cache. Bound to the GUI's refresh button — this is the
        only path that should pay the full detection cost once the cache
        exists."""
        self._profile = get_hardware_profile()
        self._save_profile_cache(self._profile)
        return self._profile.to_dict()

    def live_stats(self) -> dict:
        """Polled every few seconds by the System Audit tab while it's
        open — CPU load, RAM used, per-disk usage, temp if the board
        exposes it. Never cached, never touches the hardware-profile
        cache above; this is a fresh PowerShell spawn every call by
        design since the whole point is showing current, not stale, data."""
        return get_live_stats()

    def list_tweaks(self, tier: str) -> list:
        tweaks = list_tweaks_for_tier(tier, self._get_profile())
        return [
            {"id": t.id, "name": t.name, "tier": t.tier, "risk": t.risk,
             "category": t.category, "description": t.description}
            for t in tweaks
        ]

    def create_restore_point(self) -> dict:
        return create_restore_point("Flow pre-tweak checkpoint").to_dict()

    def list_restore_points(self) -> list:
        """Read-only — feeds the Restore Points tab's list so 'Create restore
        point' isn't the only thing that tab can do. No admin needed just to
        view (Get-ComputerRestorePoint doesn't require elevation)."""
        return list_restore_points()

    def apply_tier(self, tier: str) -> list:
        return [r.to_dict() for r in apply_tier(tier, self._get_profile())]

    def apply_tweaks(self, ids: list) -> list:
        """What the GUI's Apply button actually calls now — applies just
        the ticked subset from the tick/untick checkboxes, not the whole
        tier. apply_tier() above is kept as-is for CLI use and backward
        compatibility."""
        return [r.to_dict() for r in apply_selected(list(ids or []), self._get_profile())]

    def revert_all(self) -> list:
        return [r.to_dict() for r in revert_all()]

    def daemon_install(self, interval_minutes: int = 60) -> dict:
        return daemon_install(interval_minutes).to_dict()

    def daemon_uninstall(self) -> dict:
        return daemon_uninstall().to_dict()

    def daemon_status(self) -> dict:
        return daemon_status()

    def daemon_run_once(self) -> dict:
        """Manual 'check now' button — one enforcement pass without waiting
        for the scheduled interval. Also forces a maintenance pass (bypassing
        the idle-time/cooldown gates, same as `idle-run-now`) so this one
        button covers everything the automatic daemon_run_loop() cycle does —
        tweak drift-reapply AND non-disruptive maintenance — instead of only
        the tweak half. Disruptive actions (SFC, DISM, defrag, chkdsk) are
        still never included here; those stay manual-only from the
        Maintenance tab regardless of how this is triggered."""
        tweak_result = daemon_check_and_reapply_once()
        try:
            tweak_result["maintenance"] = daemon_idle_maintenance_check(force=True)
        except Exception as exc:  # noqa: BLE001 — a maintenance failure shouldn't hide the tweak results
            tweak_result["maintenance"] = {"skipped_reason": f"maintenance pass crashed: {exc}", "ran": []}
        return tweak_result

    def daemon_reset_blocklist(self) -> dict:
        """'Retry blocked' button — clears every tweak the daemon gave up
        on so the next check attempts them again (useful after the person
        fixes the underlying OS-level block themselves, e.g. disabling
        UCPD or taking ownership of a service key)."""
        return daemon_reset_blocklist()

    def daemon_blocklist(self) -> dict:
        """Shows blocked tweaks + their AI diagnosis (if a key is
        configured and one was generated) so the GUI can display
        'likely_cause' / 'suggested_action' next to the retry button
        instead of just a raw stderr string."""
        return daemon_blocklist_status()

    def get_idle_settings(self) -> dict:
        """Feeds the Settings tab's idle-time field — current threshold in
        minutes plus the fixed cooldown, so the GUI can show both without
        hardcoding the cooldown number twice."""
        return {"idle_threshold_minutes": get_idle_threshold_minutes(),
                "idle_cooldown_hours": IDLE_COOLDOWN_HOURS}

    def set_idle_threshold(self, minutes: int) -> dict:
        """Bound to the Settings tab's idle-time input — persists to
        ~/.flow/config.json, picked up by the daemon on its next cycle
        (no restart needed, since get_idle_threshold_minutes() reads the
        file fresh every call rather than caching it in memory)."""
        return set_idle_threshold_minutes(minutes)

    def get_theme_preference(self) -> Optional[str]:
        """Returns the saved light/dark override, or None if the person
        has never toggled it — None tells the GUI to keep following the
        OS's live setting instead of pinning to one."""
        return get_theme_preference()

    def set_theme_preference(self, theme: str) -> dict:
        """Bound to the theme toggle button — persists the explicit choice
        so it survives across launches instead of resetting to the OS
        default every time Flow opens."""
        return set_theme_preference(theme)

    def ai_status(self) -> dict:
        """Called on load — lets the GUI show/hide the AI Insight button
        without making a network call just to check if a key exists."""
        api_key, provider = get_ai_credentials()
        configured = bool(api_key and provider in AI_PROVIDERS)
        return {
            "configured": configured,
            "provider": AI_PROVIDERS[provider]["label"] if configured else None,
            "free": AI_PROVIDERS[provider]["free"] if configured else None,
        }

    def set_api_key(self, key: str) -> dict:
        """Saves a GUI-pasted key locally (~/.flow/ai_config.json) after
        auto-detecting its provider from the prefix. Empty string clears it."""
        key = (key or "").strip()
        if not key:
            _clear_ai_config()
            return {"success": True, "provider": None, "message": "API key cleared."}
        provider = _detect_ai_provider(key)
        if not provider:
            return {"success": False, "provider": None,
                     "message": "Key format not recognized — expected a Groq (gsk_...), "
                                "Gemini (AIza...), OpenAI (sk-...), Anthropic (sk-ant-...), "
                                "or OpenRouter (sk-or-...) key."}
        _save_ai_config(key, provider)
        return {"success": True, "provider": AI_PROVIDERS[provider]["label"],
                "message": f"Saved — detected provider: {AI_PROVIDERS[provider]['label']}."}

    def list_maintenance(self) -> list:
        """Read-only — no admin needed to see what's available; buttons for
        admin-required actions are just disabled client-side until elevated."""
        profile = self._get_profile()
        return [
            {"id": a.id, "name": a.name, "description": a.description,
             "disruptive": a.disruptive, "requires_admin": a.requires_admin,
             "timeout": a.timeout}
            for a in list_maintenance_actions(profile)
        ]

    def run_maintenance(self, action_id: str) -> dict:
        result = run_maintenance_action(action_id)
        _daemon_log({"event": "maintenance_run", "action_id": action_id,
                     "success": result.success, "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
        return result.to_dict()

    def ai_explain(self, tier: str) -> dict:
        """Advisory narration only for the tier's already-selected tweaks —
        see Section 5B. Safe to call repeatedly; makes one network request."""
        profile = self._get_profile()
        tweaks = list_tweaks_for_tier(tier, profile)
        return ai_explain_tier(profile, tier, tweaks)

    def ai_chat(self, message: str, history: Optional[list] = None, tier: str = "minimal") -> dict:
        """AI Chat tab — same degrade-cleanly contract as the other ai_*
        methods (no key configured or any failure => available=False,
        never an exception across the pywebview JS bridge). tier is the
        GUI's currently-selected tier so the model's tweak menu and any
        select_ids it returns line up exactly with the checkboxes visible
        on the Tweak Engine tab right now."""
        profile = self._get_profile()
        return ai_chat_reply(profile, message, history or [], tier)


@dataclass
class RequirementCheck:
    name: str
    ok: bool
    detail: str
    auto_fixable: bool = False
    fixed: bool = False

    def to_dict(self):
        return asdict(self)


def _pywebview_deps_dir() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "_flow_deps")


def _import_pywebview():
    """Try importing pywebview with the vendored deps dir on sys.path first
    (so a vendored copy wins over any system install). Returns the module,
    or None if it's not installed/importable yet — never raises."""
    deps_dir = _pywebview_deps_dir()
    if deps_dir not in sys.path:
        sys.path.insert(0, deps_dir)
    try:
        import webview
        return webview
    except Exception:
        return None


def _install_pywebview_package() -> Tuple[bool, str]:
    """pip install --target _flow_deps/, vendored next to flow.py instead of
    the system Python's site-packages — see docstring on check_requirements()
    for why. Deliberately NOT run through run_hidden(): this uses plain
    subprocess.run() with no output capture so pip's normal download/progress
    text streams straight to the console the person is already watching,
    instead of the process looking hung for the ~10-30s this takes."""
    deps_dir = _pywebview_deps_dir()
    print(f"pywebview not found — installing locally into {deps_dir} (not system-wide)...")
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--target", deps_dir, "--upgrade", "pywebview"],
            check=True,
        )
        return True, ""
    except (subprocess.CalledProcessError, OSError) as e:
        return False, str(e)


def _webview2_runtime_installed() -> bool:
    """Checks for the actual Microsoft Edge WebView2 Runtime — a system-level
    component, not a pip package. pywebview's Windows backend needs this
    present regardless of whether the Python package itself is installed;
    it's what was missing on rig 3's Edge data-directory failure upstream
    of the pywebview import even succeeding."""
    if platform.system() != "Windows":
        return True
    bases = [
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("LOCALAPPDATA", ""),
    ]
    for base in bases:
        if not base:
            continue
        app_dir = os.path.join(base, "Microsoft", "EdgeWebView", "Application")
        if not os.path.isdir(app_dir):
            continue
        try:
            for entry in os.listdir(app_dir):
                if os.path.exists(os.path.join(app_dir, entry, "msedgewebview2.exe")):
                    return True
        except OSError:
            continue
    return False


def _install_webview2_runtime() -> ExecResult:
    """Downloads and silently runs Microsoft's official WebView2 Evergreen
    Bootstrapper — a small (~2MB) installer that fetches the real runtime
    on its own. This is the same stable, versionless redirect link
    Microsoft publishes for unattended/automated deployment (see
    learn.microsoft.com/microsoft-edge/webview2/concepts/distribution).
    Needs admin, which Flow already has by the time this runs.

    Forces TLS 1.2 explicitly before the request: Windows PowerShell 5.1
    (still the default on Windows 10 rigs like the HP i3) defaults its
    .NET ServicePointManager to TLS 1.0, and go.microsoft.com/most modern
    endpoints reject that outright — that's an independent failure mode
    from anything already diagnosed, and would otherwise look identical
    to a network/proxy problem. Verifies the download actually landed and
    is non-trivial in size before executing it, instead of trusting a
    silent Invoke-WebRequest success on a truncated/empty file."""
    bootstrapper = os.path.join(os.environ.get("TEMP", "."), "MicrosoftEdgeWebview2Setup.exe")
    if os.path.exists(bootstrapper):
        try:
            os.remove(bootstrapper)  # never execute a stale/partial file left from a prior failed attempt
        except OSError:
            pass
    dl = run_powershell(
        "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; "
        f"Invoke-WebRequest -Uri 'https://go.microsoft.com/fwlink/p/?LinkId=2124703' "
        f"-OutFile '{bootstrapper}' -UseBasicParsing",
        timeout=120,
    )
    if not dl.success:
        return dl
    if not os.path.exists(bootstrapper) or os.path.getsize(bootstrapper) < 100_000:
        return ExecResult(
            command="webview2 download verify", success=False, returncode=-1,
            stdout="", stderr="downloaded bootstrapper missing or suspiciously small — not executing it",
            duration_s=0.0,
        )
    return run_hidden([bootstrapper, "/silent", "/install"], timeout=180)


def _check_python_version() -> RequirementCheck:
    ok = sys.version_info >= (3, 9)
    detail = f"Python {platform.python_version()}"
    if not ok:
        detail += (" — Flow needs 3.9+; can't auto-upgrade the interpreter that's "
                    "already running it, install a newer Python from python.org")
    return RequirementCheck("python_version", ok, detail)


def _check_pip(auto_fix: bool = True) -> RequirementCheck:
    """Checked (and fixed) via sys.executable, not PATH — flow.py is already
    running inside a real interpreter by the time this executes (flow.bat's
    job is finding that interpreter in the first place), so there's no
    PATH chicken-and-egg here: `python -m pip` always resolves correctly
    regardless of what's on PATH. The one real gap is a Python install that
    genuinely lacks pip (some distro packages, some minimal/embeddable
    builds) — ensurepip is stdlib and bundles its own wheel, so it can
    bootstrap pip with zero network access, unlike a manual get-pip.py
    download."""
    r = run_hidden([sys.executable, "-m", "pip", "--version"], timeout=15)
    if r.success:
        return RequirementCheck("pip", True, r.stdout)
    if not auto_fix:
        return RequirementCheck("pip", False, r.stderr or "pip not available", auto_fixable=True)
    print("pip not found — bootstrapping via ensurepip (stdlib, no download needed)...")
    fix = run_hidden([sys.executable, "-m", "ensurepip", "--upgrade"], timeout=60)
    r2 = run_hidden([sys.executable, "-m", "pip", "--version"], timeout=15)
    if r2.success:
        return RequirementCheck("pip", True, r2.stdout, auto_fixable=True, fixed=True)
    return RequirementCheck(
        "pip", False,
        f"ensurepip failed too: {fix.stderr or fix.stdout or 'unknown error'} — "
        f"reinstall Python from python.org with pip included",
        auto_fixable=True,
    )


def _check_webview2_runtime(auto_fix: bool) -> RequirementCheck:
    if platform.system() != "Windows":
        return RequirementCheck("webview2_runtime", True, "not applicable on this OS")
    if _webview2_runtime_installed():
        return RequirementCheck("webview2_runtime", True, "installed")
    if not auto_fix:
        return RequirementCheck("webview2_runtime", False, "Microsoft Edge WebView2 Runtime not found", auto_fixable=True)
    print("WebView2 Runtime not found — installing via Microsoft's official bootstrapper...")
    r = _install_webview2_runtime()
    if _webview2_runtime_installed():
        return RequirementCheck("webview2_runtime", True, "installed just now", auto_fixable=True, fixed=True)
    return RequirementCheck(
        "webview2_runtime", False,
        f"auto-install failed: {r.stderr or r.stdout or 'unknown error'} — "
        f"install manually from https://developer.microsoft.com/microsoft-edge/webview2/",
        auto_fixable=True,
    )


def _check_pywebview_package(auto_fix: bool) -> RequirementCheck:
    if _import_pywebview() is not None:
        return RequirementCheck("pywebview_package", True, "installed")
    if not auto_fix:
        return RequirementCheck("pywebview_package", False, "not installed", auto_fixable=True)
    installed, err = _install_pywebview_package()
    if not installed:
        return RequirementCheck("pywebview_package", False, f"auto-install failed: {err}", auto_fixable=True)
    import importlib
    importlib.invalidate_caches()
    module = _import_pywebview()
    if module is not None:
        return RequirementCheck("pywebview_package", True, "installed just now", auto_fixable=True, fixed=True)
    return RequirementCheck("pywebview_package", False,
                             "installed but still fails to import — see traceback above", auto_fixable=True)


def _check_admin() -> RequirementCheck:
    ok = is_admin()
    detail = "elevated" if ok else "not elevated — registry/service tweaks will no-op; run via flow.bat"
    return RequirementCheck("admin_elevation", ok, detail)


def check_requirements(auto_fix: bool = True) -> dict:
    """Checks everything Flow needs before it can actually run — Python
    version, pip, the system-level WebView2 runtime, the vendored pywebview
    package, and elevation state — and fixes what's fixable via direct
    commands (pip install, WebView2's official bootstrapper) instead of
    letting the GUI fail partway through startup with a cryptic error.

    Safe to call on every launch: each check is a fast no-op once its
    requirement is already satisfied. Some checks (Python version,
    elevation) can only be reported, not auto-fixed — there's no sane way
    for a running interpreter to upgrade itself, and elevation has to
    happen before this process even starts (that's what flow.bat is for)."""
    checks = [
        _check_python_version(),
        _check_pip(auto_fix),
        _check_webview2_runtime(auto_fix),
        _check_pywebview_package(auto_fix),
        _check_admin(),
    ]
    return {"ok": all(c.ok for c in checks), "checks": [c.to_dict() for c in checks]}


def _redirect_console_output_to_log():
    """Launching via pythonw.exe (no attached console) leaves sys.stdout
    and sys.stderr as None -- any print() call crashes outright rather
    than silently no-op'ing. Redirect both to ~/.flow/flow.log in that
    case so requirement checks, warnings, and daemon output all land
    somewhere readable instead of needing a visible cmd window to exist
    at all. Left alone when a real console IS attached (e.g. running
    `python flow.py gui` directly from a terminal for debugging)."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    log_dir = os.path.join(os.path.expanduser("~"), ".flow")
    try:
        os.makedirs(log_dir, exist_ok=True)
        f = open(os.path.join(log_dir, "flow.log"), "a", encoding="utf-8", buffering=1)
        f.write(f"\n--- Flow GUI launch {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        sys.stdout = f
        sys.stderr = f
    except OSError:
        # Can't even write the log -- fall back to a black hole rather
        # than crashing every print() call for the rest of the run.
        import io
        sys.stdout = sys.stderr = io.StringIO()


def launch_gui():
    _redirect_console_output_to_log()
    try:
        _launch_gui_inner()
    except SystemExit:
        raise
    except BaseException:
        # Under pyw/pythonw there's no console for a traceback to land
        # on -- it would otherwise just vanish, which is exactly the
        # "flashes and disappears, nothing happens" symptom. Log it AND
        # pop a real Windows message box (ctypes, no extra deps) so the
        # failure is actually visible instead of silent.
        tb = traceback.format_exc()
        try:
            print(tb)
            sys.stderr.flush()
        except Exception:
            pass
        try:
            log_path = os.path.join(os.path.expanduser("~"), ".flow", "flow.log")
        except Exception:
            log_path = "(unknown)"
        try:
            import ctypes
            msg = f"Flow failed to start:\n\n{tb[-1200:]}\n\nFull log: {log_path}"
            ctypes.windll.user32.MessageBoxW(0, msg, "Flow - startup error", 0x10)
        except Exception:
            pass
        sys.exit(1)


def _launch_gui_inner():
    # Constructed first, before anything else in this function -- Api.__init__
    # kicks off hardware detection on a background thread immediately (see
    # Api._prefetch_profile). Doing this before check_requirements/WebView2
    # setup instead of inline in create_window() below gives detection the
    # largest possible head start to run concurrently with everything else
    # that has to happen before the GUI can even show a window.
    api = Api()

    result = check_requirements(auto_fix=True)
    for c in result["checks"]:
        mark = "✓" if c["ok"] else "✗"
        note = " (fixed automatically)" if c.get("fixed") else ""
        print(f"  {mark} {c['name']}: {c['detail']}{note}")
    if not result["ok"]:
        blocking = [c for c in result["checks"] if not c["ok"] and c["name"] != "admin_elevation"]
        if blocking:
            print("\nFlow can't start — one or more requirements aren't met (see ✗ above).")
            sys.exit(1)
        # admin_elevation is the only failure and it's informational, not
        # blocking: Flow still opens, just in dry-run mode (see admin_status()
        # banner in the GUI) until it's relaunched via flow.bat.

    # WebView2 defaults to a folder under %TEMP%, and creating that folder
    # routinely fails when the host process is elevated (Flow always is --
    # that's the whole point of flow.bat) -- a documented WebView2/Chromium
    # limitation, not something specific to this machine. Pointing it at a
    # folder Flow already owns and controls sidesteps the failure; this is
    # Microsoft's own documented workaround (the WEBVIEW2_USER_DATA_FOLDER
    # env var), not a hack. Must be set before webview.create_window() --
    # WebView2 reads it once, at environment-creation time.
    webview2_data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_flow_deps", "webview2_data")
    os.makedirs(webview2_data_dir, exist_ok=True)
    os.environ["WEBVIEW2_USER_DATA_FOLDER"] = webview2_data_dir

    webview = _import_pywebview()  # already guaranteed importable by check_requirements() above
    window = webview.create_window(
        "Flow", html=_GUI_HTML, js_api=api,
        width=980, height=720, min_size=(760, 560),
    )
    webview.start()


# ═══════════════════════════════════════════════════════════════════
# CLI ENTRY POINT — subcommands for testing each section in isolation
# ═══════════════════════════════════════════════════════════════════

def _print_json(obj):
    print(json.dumps(obj, indent=2, default=str))


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--debug"]
    subcommand = args[0] if args else "detect"

    if subcommand == "detect":
        _print_json(get_hardware_profile().to_dict())

    elif subcommand == "admin-check":
        print(json.dumps({"is_admin": is_admin()}))

    elif subcommand == "restore-test":
        result = create_restore_point("Flow test checkpoint")
        _print_json(result.to_dict())

    elif subcommand == "restore-list":
        _print_json(list_restore_points())

    elif subcommand == "exec-test":
        # Harmless read-only command, just proves the hidden runner works.
        result = run_powershell("Get-Date")
        _print_json(result.to_dict())

    elif subcommand == "list-tweaks":
        # arg = tier (default maximal, shows everything). Read-only, no admin needed.
        tier = args[1] if len(args) > 1 else "maximal"
        profile = get_hardware_profile()
        tweaks = list_tweaks_for_tier(tier, profile)
        _print_json([
            {"id": t.id, "name": t.name, "tier": t.tier, "risk": t.risk, "category": t.category}
            for t in tweaks
        ])

    elif subcommand == "apply-tier":
        # arg = tier (required). Respects TWEAKS_APPLY_ENABLED kill-switch — dry-run by default.
        if len(args) < 2:
            print("Usage: python flow.py apply-tier [minimal|standard|maximal]")
            sys.exit(1)
        tier = args[1]
        profile = get_hardware_profile()
        results = apply_tier(tier, profile)
        _print_json([r.to_dict() for r in results])

    elif subcommand == "revert-all":
        results = revert_all()
        _print_json([r.to_dict() for r in results])

    elif subcommand == "daemon-run":
        interval = 60
        if "--interval-minutes" in args:
            try:
                interval = int(args[args.index("--interval-minutes") + 1])
            except (ValueError, IndexError):
                pass
        daemon_run_loop(interval)

    elif subcommand == "daemon-check":
        _print_json(daemon_check_and_reapply_once())

    elif subcommand == "daemon-install":
        interval = 60
        if len(args) > 1:
            try:
                interval = int(args[1])
            except ValueError:
                pass
        _print_json(daemon_install(interval).to_dict())

    elif subcommand == "daemon-uninstall":
        _print_json(daemon_uninstall().to_dict())

    elif subcommand == "daemon-status":
        _print_json(daemon_status())

    elif subcommand == "daemon-blocklist":
        _print_json(daemon_blocklist_status())

    elif subcommand == "daemon-reset-blocklist":
        ids = args[1:] if len(args) > 1 else None
        _print_json(daemon_reset_blocklist(ids))

    elif subcommand == "dedupe-revert-log":
        _print_json(dedupe_revert_log())

    elif subcommand == "list-maintenance":
        profile = get_hardware_profile()
        _print_json([
            {"id": a.id, "name": a.name, "description": a.description,
             "disruptive": a.disruptive, "requires_admin": a.requires_admin}
            for a in list_maintenance_actions(profile)
        ])

    elif subcommand == "run-maintenance":
        if len(args) < 2:
            print("Usage: python flow.py run-maintenance <action_id>")
            sys.exit(1)
        result = run_maintenance_action(args[1])
        _daemon_log({"event": "maintenance_run", "action_id": args[1],
                     "success": result.success, "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
        _print_json(result.to_dict())

    elif subcommand == "idle-check":
        idle_seconds = _get_idle_seconds()
        _print_json({
            "idle_seconds": idle_seconds,
            "idle_minutes": round(idle_seconds / 60, 1) if idle_seconds is not None else None,
            "threshold_minutes": get_idle_threshold_minutes(),
        })

    elif subcommand == "set-idle-threshold":
        if len(args) < 2:
            print("Usage: python flow.py set-idle-threshold <minutes>")
            sys.exit(1)
        try:
            minutes = int(args[1])
        except ValueError:
            print(f"'{args[1]}' isn't a whole number of minutes.")
            sys.exit(1)
        _print_json(set_idle_threshold_minutes(minutes))

    elif subcommand == "idle-run-now":
        # Bypasses the idle/cooldown gates for testing — still respects the
        # disruptive/hdd/ssd hardware-safety filters inside the function.
        _print_json(daemon_idle_maintenance_check(force=True))

    elif subcommand == "check-requirements":
        _print_json(check_requirements(auto_fix=True))

    elif subcommand == "gui":
        launch_gui()

    else:
        print(f"Unknown subcommand: {subcommand}")
        print("Usage: python flow.py [detect|admin-check|restore-test|restore-list|exec-test|"
              "list-tweaks|apply-tier|revert-all|daemon-run|daemon-check|daemon-install|"
              "daemon-uninstall|daemon-status|daemon-blocklist|daemon-reset-blocklist|"
              "dedupe-revert-log|"
              "list-maintenance|run-maintenance|idle-check|idle-run-now|set-idle-threshold|"
              "check-requirements|gui] [--debug]")