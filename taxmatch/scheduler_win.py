"""Καθημερινός έλεγχος μέσω Windows Task Scheduler (τρέχει ανεξάρτητα από το αν είναι ανοιχτό το UI).

Το task δημιουργείται ΓΙΑ ΤΟΝ ΤΡΕΧΟΝΤΑ ΧΡΗΣΤΗ (χωρίς admin, χωρίς αποθήκευση κωδικού Windows):
* StartWhenAvailable: αν ο υπολογιστής ήταν κλειστός την ώρα εκτέλεσης, τρέχει μόλις ανοίξει.
* RunOnlyIfNetworkAvailable, δεν σταματά με μπαταρία, όριο 1 ώρα, IgnoreNew αν τρέχει ήδη.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape

from . import config

TASK_NAME = "TaxMatch Daily Check"
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def supported() -> bool:
    return os.name == "nt"


def task_xml(command: str, arguments: str, hhmm: str, start: date | None = None) -> str:
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", hhmm):
        raise ValueError("Η ώρα πρέπει να είναι της μορφής ΩΩ:ΛΛ")
    start = start or date.today()
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>TaxMatch by ScanMyData — καθημερινός έλεγχος φορολογικών νέων</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{start.isoformat()}T{hhmm}:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(command)}</Command>
      <Arguments>{escape(arguments)}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, encoding="oem", errors="replace",
                          creationflags=_CREATE_NO_WINDOW, timeout=60)


def install(hhmm: str = "08:00") -> tuple[bool, str]:
    if not supported():
        return False, "Το Task Scheduler είναι διαθέσιμο μόνο στα Windows."
    cmd = config.executable_command()
    command, extra = cmd[0], cmd[1:]
    arguments = " ".join([*(f'"{a}"' if " " in a else a for a in extra), "--daily"])
    xml = task_xml(command, arguments, hhmm)
    fd, tmp = tempfile.mkstemp(suffix=".xml")
    try:
        with os.fdopen(fd, "w", encoding="utf-16") as fh:      # το schtasks /XML απαιτεί UTF-16
            fh.write(xml)
        res = _run(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", tmp, "/F"])
    finally:
        Path(tmp).unlink(missing_ok=True)
    if res.returncode != 0:
        return False, (res.stderr or res.stdout).strip()[:300] or "Αποτυχία δημιουργίας task"
    return True, f"Ο καθημερινός έλεγχος προγραμματίστηκε για τις {hhmm}."


def remove() -> tuple[bool, str]:
    if not supported():
        return False, "Μόνο σε Windows."
    if not is_installed():
        return True, "Δεν υπήρχε προγραμματισμένος έλεγχος."
    res = _run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
    if res.returncode != 0:
        return False, (res.stderr or res.stdout).strip()[:300]
    return True, "Ο προγραμματισμένος έλεγχος αφαιρέθηκε."


def is_installed() -> bool:
    if not supported():
        return False
    try:
        return _run(["schtasks", "/Query", "/TN", TASK_NAME]).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
