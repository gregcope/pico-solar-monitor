import machine
import os
import time

flag_file: str = "ota_pending.flag"
main_file: str = "main.py"


def write_crash_log(error_name: str, error_detail: str) -> None:
    """Write error details to a local log file, overwriting previous entries."""
    try:
        with open("boot_error.log", "w") as log_file:
            log_file.write(f"Boot failed due to {error_name}: {error_detail}\n")
    except OSError:
        pass


def trigger_rollback() -> None:
    """Attempt to restore the backup main.py and reset the device."""
    try:
        try:
            os.remove(main_file)
        except OSError as remove_err:
            if remove_err.args[0] != 2:
                raise remove_err

        os.rename("main_backup.py", main_file)
        
        try:
            os.remove(flag_file)
        except OSError:
            pass
            
        machine.reset()

    except OSError as rollback_error:
        write_crash_log("RollbackError", f"Errno {rollback_error.args[0]}")
        time.sleep(300)
        machine.reset()


def main_exists() -> bool:
    """Check if main.py is present on the filesystem."""
    try:
        os.stat(main_file)
        return True
    except OSError:
        return False


time.sleep(1)

# If main.py does not exist yet (initial bootstrap), exit boot.py 
# cleanly to allow REPL access for ugit.pull_all()
if not main_exists():
    raise SystemExit


# Check for pending OTA flag
try:
    os.stat(flag_file)
    ota_is_pending: bool = True
except OSError:
    ota_is_pending = False


# If WDT fired while OTA was pending, the new firmware failed to boot
if ota_is_pending and machine.reset_cause() == machine.WDT_RESET:
    write_crash_log("WatchdogTimeout", "WDT reset during pending OTA")
    trigger_rollback()


# Otherwise, exit boot.py cleanly and let MicroPython run main.py automatically
