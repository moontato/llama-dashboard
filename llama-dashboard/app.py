import os
import shutil
import logging
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, Response, g, send_file

from src.db import init_db
from src.settings import load_settings, get_settings, save_settings, Settings
from src.ini_store import IniStore
from src.presets import Preset
from src.models_fs import get_model_list, get_mmproj_list
from src.cmd_builder import build_argv, build_command_string
from src.server_ctrl import controller
from src.hf_download import downloads
from src.logs import get_log_files, get_current_log, tail_sse
from src.system_stats import get_latest, start_stats_thread

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = "llama-dashboard-secret-change-me"

@app.template_filter('build_command')
def template_build_command(preset_name):
    return build_command_string(preset_name)


def _get_app_dir():
    return Path(__file__).resolve().parent


# Initialize on startup
app_dir = _get_app_dir()
settings = load_settings(app_dir)
init_db()
controller.reconcile()
start_stats_thread()

NAV_ITEMS = [
    ("dashboard", "Dashboard"),
    ("presets_list", "Presets"),
    ("models", "Models"),
    ("mmproj", "MMProj"),
    ("downloads_page", "Downloads"),
    ("logs_page", "Logs"),
    ("settings_page", "Settings"),
]


# ---- Template context processor ----
@app.context_processor
def inject_settings():
    try:
        s = get_settings()
        # Compute relative log_dir for editing
        try:
            rel_log = os.path.relpath(s.log_dir, app_dir)
        except ValueError:
            rel_log = s.log_dir
        return {"settings": s, "NAV_ITEMS": NAV_ITEMS, "rel_log_dir": rel_log}
    except RuntimeError:
        return {"settings": None, "NAV_ITEMS": NAV_ITEMS, "rel_log_dir": "./logs"}


# ---- Dashboard ----
@app.route("/")
def dashboard():
    server_state = controller.get_state()
    presets = _get_preset_names()
    stats = get_latest()
    recent_downloads = downloads.get_history(limit=3)
    return render_template(
        "dashboard.html",
        server_state=server_state,
        presets=presets,
        stats=stats,
        recent_downloads=recent_downloads,
    )


@app.route("/api/system_stats")
def api_system_stats():
    stats = get_latest()
    return render_template("partials/_system_stats.html", stats=stats)


# ---- Presets ----
@app.route("/presets")
def presets_list():
    presets = _get_presets()
    models = get_model_list(settings)
    mmproj_list = get_mmproj_list(settings)
    return render_template(
        "presets/list.html",
        presets=presets,
        models=models,
        mmproj_list=mmproj_list,
    )


@app.route("/presets/new", methods=["GET", "POST"])
def preset_new():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Preset name is required.", "error")
            return render_template("presets/edit.html", preset=None, global_defaults=None, is_new=True)

        store = IniStore(settings.models_ini_path)
        if store.get_section(name):
            flash(f"Preset '{name}' already exists.", "error")
            return render_template("presets/edit.html", preset=None, global_defaults=None, is_new=True)

        keys = _form_to_keys(request.form)
        from collections import OrderedDict
        ordered = OrderedDict()
        for k in keys:
            ordered[k] = keys[k]

        store.set_section(name, ordered)
        store.save()
        flash(f"Preset '{name}' created.", "success")
        return redirect(url_for("presets_list"))

    return render_template("presets/edit.html", preset=None, global_defaults=None, is_new=True)


@app.route("/presets/<name>/edit", methods=["GET", "POST"])
def preset_edit(name):
    store = IniStore(settings.models_ini_path)
    section = store.get_section(name)

    if request.method == "POST":
        keys = _form_to_keys(request.form)
        from collections import OrderedDict
        ordered = OrderedDict()
        for k in keys:
            ordered[k] = keys[k]

        store.set_section(name, ordered)
        store.save()
        flash(f"Preset '{name}' saved.", "success")
        return redirect(url_for("presets_list"))

    global_defaults = store.get_global_defaults()
    return render_template(
        "presets/edit.html",
        preset=Preset.from_section(name, section) if section else None,
        global_defaults=global_defaults,
        is_new=False,
    )


@app.route("/presets/<name>/clone", methods=["POST"])
def preset_clone(name):
    store = IniStore(settings.models_ini_path)
    section = store.get_section(name)
    if not section:
        flash("Preset not found.", "error")
        return redirect(url_for("presets_list"))

    new_name = f"{name}-copy"
    existing = store.get_section(new_name)
    if existing:
        new_name = f"{new_name}-2"

    from collections import OrderedDict
    store.set_section(new_name, OrderedDict(section))
    store.save()
    flash(f"Cloned as '{new_name}'.", "success")
    return redirect(url_for("presets_list"))


@app.route("/presets/<name>/delete", methods=["POST"])
def preset_delete(name):
    store = IniStore(settings.models_ini_path)
    confirm = request.form.get("confirm", "")

    if confirm != name:
        flash("Preset name confirmation does not match.", "error")
        return redirect(url_for("presets_list"))

    store.delete_section(name)
    store.save()
    flash(f"Preset '{name}' deleted.", "success")
    return redirect(url_for("presets_list"))


@app.route("/presets/<name>/launch", methods=["POST"])
def preset_launch(name):
    result = controller.start(name)
    if "error" in result:
        flash(result["error"], "error")
    else:
        flash(f"Started preset '{name}' (PID {result.get('pid', '?')}).", "success")
    return redirect(url_for("dashboard"))


@app.route("/presets/<name>/command")
def preset_command(name):
    cmd = build_command_string(name)
    return cmd


@app.route("/presets/global/edit", methods=["GET", "POST"])
def preset_global_edit():
    store = IniStore(settings.models_ini_path)
    section = store.get_global_defaults()

    if request.method == "POST":
        keys = _form_to_keys(request.form)
        from collections import OrderedDict
        ordered = OrderedDict()
        for k in keys:
            ordered[k] = keys[k]

        store.set_section("*", ordered)
        store.save()
        flash("Global defaults saved.", "success")
        return redirect(url_for("presets_list"))

    return render_template(
        "presets/edit.html",
        preset=Preset.from_section("*", section) if section else None,
        global_defaults=section,
        is_new=False,
        is_global=True,
    )


@app.route("/server/stop", methods=["POST"])
def server_stop():
    result = controller.stop()
    if "error" in result:
        flash(result["error"], "error")
    else:
        flash("Server stopped.", "success")
    return redirect(url_for("dashboard"))


@app.route("/server/restart", methods=["POST"])
def server_restart():
    state = controller.get_state()
    preset = state.get("preset", "")
    if not preset:
        flash("No server running to restart.", "error")
        return redirect(url_for("dashboard"))
    result = controller.restart(preset)
    if "error" in result:
        flash(result.get("error", "Restart failed."), "error")
    else:
        flash(f"Restarted preset '{preset}'.", "success")
    return redirect(url_for("dashboard"))


# ---- Models ----
@app.route("/models")
def models():
    models = get_model_list(settings)
    try:
        usage = shutil.disk_usage(settings.models_dir)
        disk_info = {
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
            "free_human": _human_size(usage.free),
        }
    except Exception:
        disk_info = None
    return render_template("models/list.html", models=models, disk_info=disk_info)


@app.route("/models/<filename>/rename", methods=["POST"])
def model_rename(filename):
    new_name = request.form.get("new_name", "").strip()
    if not new_name:
        flash("New name is required.", "error")
        return redirect(url_for("models"))

    old_path = os.path.join(settings.models_dir, filename)
    new_path = os.path.join(settings.models_dir, new_name)

    if not os.path.exists(old_path):
        flash("Model file not found.", "error")
        return redirect(url_for("models"))

    os.rename(old_path, new_path)

    # Update presets that reference this file
    store = IniStore(settings.models_ini_path)
    updated = False
    for preset_name in store.section_names():
        section = store.get_section(preset_name)
        if section and (section.get("model") == old_path or os.path.basename(section.get("model", "")) == filename):
            section["model"] = new_path
            updated = True
    if updated:
        store.save()

    flash(f"Renamed '{filename}' to '{new_name}'.", "success")
    return redirect(url_for("models"))


@app.route("/models/<filename>/delete", methods=["POST"])
def model_delete(filename):
    force = request.form.get("force") == "on"
    old_path = os.path.join(settings.models_dir, filename)

    # Check used_by
    models = get_model_list(settings)
    model_info = None
    for m in models:
        if m["filename"] == filename:
            model_info = m
            break

    if model_info and model_info["used_by"] and not force:
        flash(
            f"File is used by: {', '.join(model_info['used_by'])}. Check 'Force delete' to remove anyway.",
            "error",
        )
        return redirect(url_for("models"))

    try:
        os.remove(old_path)

        # Remove from presets
        store = IniStore(settings.models_ini_path)
        updated = False
        for preset_name in store.section_names():
            section = store.get_section(preset_name)
            if section and section.get("model") == old_path:
                section["model"] = ""
                updated = True
        if updated:
            store.save()

        flash(f"Deleted '{filename}'.", "success")
    except OSError as e:
        flash(f"Delete failed: {e}", "error")

    return redirect(url_for("models"))


# ---- MMProj ----
@app.route("/mmproj")
def mmproj():
    mmproj_list = get_mmproj_list(settings)
    try:
        usage = shutil.disk_usage(settings.mmproj_dir)
        disk_info = {
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
            "free_human": _human_size(usage.free),
        }
    except Exception:
        disk_info = None
    return render_template("mmproj/list.html", models=mmproj_list, disk_info=disk_info)


@app.route("/mmproj/<filename>/rename", methods=["POST"])
def mmproj_rename(filename):
    new_name = request.form.get("new_name", "").strip()
    if not new_name:
        flash("New name is required.", "error")
        return redirect(url_for("mmproj"))

    old_path = os.path.join(settings.mmproj_dir, filename)
    new_path = os.path.join(settings.mmproj_dir, new_name)

    if not os.path.exists(old_path):
        flash("MMProj file not found.", "error")
        return redirect(url_for("mmproj"))

    os.rename(old_path, new_path)

    store = IniStore(settings.models_ini_path)
    updated = False
    for preset_name in store.section_names():
        section = store.get_section(preset_name)
        if section and (section.get("mmproj") == old_path or os.path.basename(section.get("mmproj", "")) == filename):
            section["mmproj"] = new_path
            updated = True
    if updated:
        store.save()

    flash(f"Renamed '{filename}' to '{new_name}'.", "success")
    return redirect(url_for("mmproj"))


@app.route("/mmproj/<filename>/delete", methods=["POST"])
def mmproj_delete(filename):
    force = request.form.get("force") == "on"
    old_path = os.path.join(settings.mmproj_dir, filename)

    mmproj_list = get_mmproj_list(settings)
    model_info = None
    for m in mmproj_list:
        if m["filename"] == filename:
            model_info = m
            break

    if model_info and model_info["used_by"] and not force:
        flash(
            f"File is used by: {', '.join(model_info['used_by'])}. Check 'Force delete' to remove anyway.",
            "error",
        )
        return redirect(url_for("mmproj"))

    try:
        os.remove(old_path)

        store = IniStore(settings.models_ini_path)
        updated = False
        for preset_name in store.section_names():
            section = store.get_section(preset_name)
            if section and section.get("mmproj") == old_path:
                section["mmproj"] = ""
                updated = True
        if updated:
            store.save()

        flash(f"Deleted '{filename}'.", "success")
    except OSError as e:
        flash(f"Delete failed: {e}", "error")

    return redirect(url_for("mmproj"))


# ---- Downloads ----
@app.route("/downloads")
def downloads_page():
    queue = downloads.get_queue()
    history = downloads.get_history(limit=50)
    return render_template("downloads/list.html", queue=queue, history=history)


@app.route("/downloads/queue", methods=["POST"])
def downloads_enqueue():
    source = request.form.get("source", "").strip()
    dest_type = request.form.get("dest", "models")
    custom_dest = request.form.get("custom_dest", "").strip()

    if not source:
        flash("Source is required.", "error")
        return redirect(url_for("downloads_page"))

    if dest_type == "custom" and not custom_dest:
        flash("Custom destination is required.", "error")
        return redirect(url_for("downloads_page"))

    if dest_type == "custom":
        dest = custom_dest
    elif dest_type == "mmproj":
        dest = settings.mmproj_dir
    else:
        dest = settings.models_dir

    result = downloads.queue_download(source, dest)
    if "error" in result:
        flash(result["error"], "error")
    elif result.get("list_files"):
        # Show file picker
        files = downloads.list_repo_files(result["repo_id"])
        flash(f"Found {len(files)} .gguf files in {result['repo_id']}. Select one to download.", "info")
    else:
        flash(f"Download queued (ID {result['id']}).", "success")

    return redirect(url_for("downloads_page"))


@app.route("/downloads/<int:download_id>/cancel", methods=["POST"])
def downloads_cancel(download_id):
    result = downloads.cancel_download(download_id)
    if "error" in result:
        flash(result["error"], "error")
    else:
        flash("Cancel requested.", "info")
    return redirect(url_for("downloads_page"))


@app.route("/downloads/<int:download_id>")
def download_status(download_id):
    d = downloads.get_download(download_id)
    if not d:
        return render_template("partials/_download_row.html", download=None)
    return render_template("partials/_download_row.html", download=d)


# ---- Logs ----
@app.route("/logs")
def logs_page():
    log_files = get_log_files(settings.log_dir)
    current = get_current_log(settings.log_dir)
    return render_template("logs/list.html", log_files=log_files, current_log=current)


@app.route("/logs/stream")
def logs_stream():
    log_file = request.args.get("file", "")
    if not log_file:
        log_file = get_current_log(settings.log_dir)
    if not log_file:
        return Response("data: [No log file available]\n\n", mimetype="text/event-stream")

    log_path = os.path.join(settings.log_dir, log_file)
    if not os.path.exists(log_path):
        return Response("data: [Log file not found]\n\n", mimetype="text/event-stream")

    follow = request.args.get("follow", "1") == "1"
    return Response(
        tail_sse(log_path, follow=follow),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/logs/download")
def logs_download():
    log_file = request.args.get("file", "")
    if not log_file:
        log_file = get_current_log(settings.log_dir)
    if not log_file:
        flash("No log file to download.", "error")
        return redirect(url_for("logs_page"))

    log_path = os.path.join(settings.log_dir, log_file)
    return send_file(log_path, as_attachment=True, download_name=log_file)


# ---- Settings ----
@app.route("/settings")
def settings_page():
    try:
        s = get_settings()
    except RuntimeError:
        s = Settings()
    return render_template("settings/index.html", settings=s)


@app.route("/settings", methods=["POST"])
def settings_save():
    raw_log_dir = request.form.get("log_dir", "./logs")
    if not os.path.isabs(raw_log_dir):
        log_dir = str(app_dir / raw_log_dir)
    else:
        log_dir = raw_log_dir

    s = Settings(
        llama_server_bin=request.form.get("llama_server_bin", ""),
        llama_server_host=request.form.get("llama_server_host", "0.0.0.0"),
        llama_server_port=int(request.form.get("llama_server_port", 8080)),
        dashboard_host=request.form.get("dashboard_host", "0.0.0.0"),
        dashboard_port=int(request.form.get("dashboard_port", 8734)),
        models_dir=request.form.get("models_dir", ""),
        mmproj_dir=request.form.get("mmproj_dir", ""),
        models_ini_path=request.form.get("models_ini_path", ""),
        log_dir=log_dir,
        allow_multiple_servers=request.form.get("allow_multiple_servers") == "on",
        hf_token=request.form.get("hf_token", ""),
        boolean_flag_keys=[
            k.strip() for k in request.form.get("boolean_flag_keys", "").split(",") if k.strip()
        ],
    )

    # Validate paths
    warnings = []
    if s.models_dir and not os.path.isdir(s.models_dir):
        warnings.append(f"Models directory '{s.models_dir}' does not exist.")
    if s.mmproj_dir and not os.path.isdir(s.mmproj_dir):
        warnings.append(f"MMProj directory '{s.mmproj_dir}' does not exist.")
    if s.models_ini_path and not os.path.isfile(s.models_ini_path):
        warnings.append(f"Models INI file '{s.models_ini_path}' does not exist.")

    save_settings(s)
    for w in warnings:
        flash(w, "warning")
    flash("Settings saved.", "success")
    return redirect(url_for("settings_page"))


@app.route("/settings/test-binary", methods=["POST"])
def settings_test_binary():
    import subprocess
    bin_path = request.form.get("bin", "")
    if not bin_path:
        return jsonify({"error": "No binary path provided."})

    try:
        result = subprocess.run(
            [bin_path, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return jsonify({
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "returncode": result.returncode,
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Command timed out (5s)."})
    except FileNotFoundError:
        return jsonify({"error": f"Binary not found: {bin_path}"})
    except Exception as e:
        return jsonify({"error": str(e)})


# ---- Helpers ----
def _get_preset_names():
    try:
        store = IniStore(settings.models_ini_path)
        return store.section_names()
    except Exception:
        return []


def _get_presets():
    presets = []
    try:
        store = IniStore(settings.models_ini_path)
        for name in store.section_names():
            section = store.get_section(name)
            if section:
                presets.append(Preset.from_section(name, section))
    except Exception as e:
        logger.error("Failed to load presets: %s", e)
    return presets


def _form_to_keys(form):
    """Extract key-value pairs from form, preserving order."""
    keys = {}
    prefix = "key_"
    # Collect all key_XX entries in order
    key_names = []
    for k in form.keys():
        if k.startswith(prefix):
            idx = k[len(prefix):]
            key_names.append((int(idx), form.getlist(k)[0]))

    for _, k in sorted(key_names):
        v = form.getlist(f"value_{k}")
        if v:
            keys[k] = v[0]
        else:
            keys[k] = ""
    return keys


def _human_size(n):
    for unit in ("B", "Ki", "Mi", "Gi", "Ti"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} Pi"


if __name__ == "__main__":
    try:
        s = get_settings()
        app.run(
            host=s.dashboard_host,
            port=s.dashboard_port,
            debug=os.environ.get("FLASK_DEBUG") == "1",
        )
    except Exception:
        app.run(host="0.0.0.0", port=8734, debug=True)
