import time
import threading
import logging

logger = logging.getLogger(__name__)

_latest: dict | None = None
_latest_lock = threading.Lock()
_jtop_available = False


def get_latest() -> dict | None:
    """Get the latest system stats snapshot. Never call jtop from request handlers."""
    with _latest_lock:
        return _latest.copy() if _latest else None


def _extract_gpu(jetson):
    """Extract GPU utilization from jtop, handling different version formats."""
    try:
        gpu = jetson.gpu
        if isinstance(gpu, list):
            for g in gpu:
                if isinstance(g, dict) and "gpu" in g:
                    return g["gpu"]
                if isinstance(g, dict) and "util" in g:
                    return g["util"]
            if gpu and isinstance(gpu[0], dict):
                return gpu[0].get("gpu", gpu[0].get("util", 0))
        elif isinstance(gpu, dict):
            return gpu.get("gpu", gpu.get("util", 0))
    except Exception:
        pass
    return None


def _extract_power(jetson):
    """Extract power draw in watts from jtop."""
    try:
        power = jetson.power
        if isinstance(power, dict):
            # Some versions have 'vdd_cpu' etc.
            total = 0.0
            for key in ("vdd_cpu", "vdd_gpu", "vdd_ram", "vdd_disk", "total"):
                if key in power:
                    val = power[key]
                    if isinstance(val, (int, float)):
                        # Some versions report mW
                        if val > 10000:
                            val = val / 1000.0
                        total += val
            if total > 0:
                return total
            # Fallback
            if "inquiry" in power:
                return power["inquiry"]
            if "vcin" in power and "vdd_5in" in power:
                # Guess from voltage/current
                pass
        elif isinstance(power, (int, float)):
            val = power
            if val > 10000:
                val = val / 1000.0
            return val
    except Exception:
        pass
    return None


def _extract_memory(jetson):
    """Extract unified memory info."""
    try:
        mem = jetson.memory
        if isinstance(mem, dict):
            ram = mem.get("RAM", {})
            if isinstance(ram, dict):
                used = ram.get("used", 0)
                total = ram.get("total", 1)
                # Convert to MiB if needed (some versions use KB)
                if total > 1000000:
                    used = used / 1024.0
                    total = total / 1024.0
                return {"used_mb": used, "total_mb": total}
    except Exception:
        pass
    return None


def _extract_temperature(jetson):
    """Extract highest temperature."""
    max_temp = None
    try:
        temps = jetson.temperature
        if isinstance(temps, list):
            for t in temps:
                if isinstance(t, dict):
                    temp_val = t.get("temp", t.get("temperature"))
                    if temp_val is not None and isinstance(temp_val, (int, float)):
                        if 0 < temp_val < 150:
                            if max_temp is None or temp_val > max_temp:
                                max_temp = temp_val
                elif isinstance(t, (int, float)):
                    if 0 < t < 150:
                        if max_temp is None or t > max_temp:
                            max_temp = t
    except Exception:
        pass
    return max_temp


def _build_snapshot(jetson) -> dict:
    gpu = _extract_gpu(jetson)
    power = _extract_power(jetson)
    memory = _extract_memory(jetson)
    temp = _extract_temperature(jetson)

    snapshot = {"ts": time.time()}
    if gpu is not None:
        snapshot["gpu_util_pct"] = int(gpu)
    if memory:
        snapshot["mem_used_mb"] = memory["used_mb"]
        snapshot["mem_total_mb"] = memory["total_mb"]
    if power is not None:
        snapshot["power_w"] = round(power, 1)
    if temp is not None:
        snapshot["temp_c"] = round(temp, 1)
    return snapshot


def _stats_thread():
    global _latest, _jtop_available
    logger.info("System stats thread started")

    while True:
        try:
            from jtop import jtop
            with jtop() as jetson:
                while jetson.ok():
                    try:
                        snapshot = _build_snapshot(jetson)
                        with _latest_lock:
                            _latest = snapshot
                        _jtop_available = True
                    except Exception as e:
                        logger.warning("Error building snapshot: %s", e)
        except Exception as e:
            _jtop_available = False
            with _latest_lock:
                _latest = {"error": str(e)}
            logger.warning("jtop connection failed: %s. Retrying in 30s...", e)

        time.sleep(30)


_thread: threading.Thread | None = None


def start_stats_thread():
    """Start the background jtop reader thread."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _thread = threading.Thread(target=_stats_thread, daemon=True, name="system-stats")
    _thread.start()
    logger.info("System stats thread started")


def stop_stats_thread():
    """Stop the background thread."""
    global _thread
    _thread = None
