# RavenLib Sync Demo

This folder is a reduced, working demo of RavenLib. It keeps the server dashboard and the basic project synchronization flow, while excluding old builds, private deployment files, duplicate clients and unrelated product extensions.

Included:

- server dashboard at `/` with first-run password setup;
- manifest-based project synchronization with SHA-256 objects;
- web GUI client from `Client/web_client.py` and `Client/webui/`;
- Windows client build at `Client/dist/RavenLibClient.exe`;
- automatic server restart through `Server/ravenlib-server.service`.

The package is shipped in a fresh state: no dashboard password, projects, manifests, uploaded objects, logs or machine-specific configuration are included. The first user creates the dashboard password on the first visit.

## License

This demo version is licensed under [CC BY-NC 4.0](LICENSE.md). Commercial use is not permitted. Third-party components retain their own licenses.

## Requirements

- Python 3.11 or newer;
- pywebview for running the source desktop-style web GUI;
- FastAPI and Uvicorn for the server, installed from `Server/requirements.txt`.

## Install and run the server

From the `Github ready` directory:

```powershell
cd Server
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

Open the dashboard at:

```text
http://127.0.0.1:8000/
```

On first opening, create the server dashboard password. The server stores runtime data in `Server/manifests/`, `Server/objects/`, `Server/server.log` and `Server/server_config.json`; these files are generated locally and are not part of the public source package.

For a remote client, replace `127.0.0.1` with the server address and allow TCP port `8000` through the firewall.

### Linux auto-restart

`Server/ravenlib-server.service` assumes the project is installed at `~/RavenLib` and the virtual environment is `Server/.venv`:

```bash
sudo install -m 644 Server/ravenlib-server.service /etc/systemd/system/ravenlib-server.service
sudo systemctl daemon-reload
sudo systemctl enable --now ravenlib-server
sudo systemctl status ravenlib-server
```

The service uses `Restart=always` and waits three seconds before restarting. Adjust `WorkingDirectory` and `ExecStart` for another installation path.

## Run the web client from Python

In another terminal:

```powershell
cd Client
python -m pip install -r requirements.txt
python web_client_window.py
```

The client starts a local web interface in an embedded desktop window. Enter the server URL, project name and project folder path manually, then use `Scan and Sync`, `Pull Latest` or `Refresh status`.

To run only the local UI server and open it in a browser manually:

```powershell
python web_client_window.py --no-window
```

The demo client performs a complete folder scan on every sync. It stores its local manifest as `.ravenlib/manifest.json` inside the entered project folder and skips `.git`, virtual environments, build folders, `dist` and Python cache folders. The client keeps only basic sync, pull and optional automatic retry functionality; server administration remains in the dashboard.

## Run the Windows EXE

Double-click `Client/dist/RavenLibClient.exe`. It opens the same embedded web GUI without installing Python.

## Rebuild the one client EXE

```powershell
cd Client
python -m pip install pyinstaller
.\build_client.ps1
```

The only distributable build is `Client/dist/RavenLibClient.exe`; PyInstaller work files are written to the system temporary directory.

## Dashboard and sync endpoints

The dashboard uses the server's existing authenticated dashboard API. The client synchronization flow uses:

```text
GET  /api/connection/status
POST /sync/check
POST /sync/finalize/{sync_id}
GET  /sync/manifest/{project}
PUT  /objects/{sha256}?sync_id={sync_id}
GET  /objects/{sha256}
```
