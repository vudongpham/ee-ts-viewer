# Installation guide

This guide walks through every step, from a fresh Google account to the running viewer. If you already have Earth Engine access and only need the commands, see the [README](README.md).

The app needs two things from Google:

1. **A Google Cloud project that is registered for Earth Engine.** All Earth Engine requests are billed to (free for non-commercial use) and counted against this project. You pass its ID with `--project`.
2. **Credentials** that prove who you are: either your own Google login (saved once in `~/.config/earthengine`) or a service account JSON key.

These two are separate. In particular, the project you log in *through* does not have to be the project you run the app *with* (see [Step 2](#step-2--log-in-once)).

---

## Step 1 — Create and register a Cloud project

1. Open <https://code.earthengine.google.com/register> and sign in with your Google account.
2. Choose **Use with a Cloud project** → **Unpaid usage** and select the project type that fits (e.g. *Academia & Research* or *Personal / hobby*).
3. Either **create a new Cloud project** (recommended — pick an ID such as `ee-yourname`) or select an existing one.
4. Click **Continue to summary** → **Confirm**. This registers the project and enables the *Earth Engine API* on it.

Note the **project ID** (not the display name), e.g. `ee-yourname`. You can see it at <https://console.cloud.google.com/home/dashboard>.

Check: open <https://console.cloud.google.com/apis/library/earthengine.googleapis.com?project=ee-yourname> — it should say **API Enabled**. If not, click **Enable**.

> **Work or university account?** Your organisation may block project creation or Earth Engine. Ask your admin, or create the project with a personal Google account and add your work account as *Owner* under *IAM & Admin → IAM*.

---

## Step 2 — Log in once

Pick **one** of the three options. The result of A and B is the same file, `~/.config/earthengine/credentials`, which is then mounted into the Docker container.

| Option | Needs | Good for |
|---|---|---|
| **A. Log in on your own computer** (recommended) | Python | Avoids the OAuth2 error below entirely |
| **B. Log in inside Docker** | Docker only | Machines without Python |
| **C. Service account key** | Cloud Console access | Servers, shared machines, no browser |

### Option A — log in on your own computer (recommended)

Install the Earth Engine client and log in. This uses the client's built-in login, so no OAuth setup on your project is needed:

```bash
pip install earthengine-api
```

```bash
earthengine authenticate --auth_mode=localhost
```

A browser tab opens; sign in with the Google account that has access to the project and allow access. The terminal prints `Successfully saved authorization token.`

If you are on a remote machine over SSH (no browser), use the `gcloud` mode instead (needs the [gcloud CLI](https://cloud.google.com/sdk/docs/install)): `earthengine authenticate --auth_mode=gcloud`, or log in on your own computer and copy `~/.config/earthengine/credentials` to the same path on the server.

### Option B — log in inside Docker

```bash
docker run -it --rm \
  -v ~/.config/earthengine:/root/.config/earthengine \
  vudongpham/eetsviewer earthengine authenticate --auth_mode=notebook
```

1. The command prints a URL. Open it in your browser.
2. On the *Notebook Authenticator* page, choose a **Cloud project** and click **Generate Token**. Sign in and allow all requested permissions.
3. Copy the code shown at the end and paste it back into the terminal.

You must be *Owner* or *Editor* of the project you choose on this page, because the authenticator adds an OAuth client to it the first time.

#### "Project has an incompatible OAuth2 Client configuration"

This is the most common problem. It appears in Option B when the project you selected on the *Notebook Authenticator* page **already has an OAuth consent screen / OAuth client** from something else (another app, a Colab or Apps Script setup, an earlier experiment). A Cloud project can have only one OAuth configuration and it cannot be deleted, so that project can never be used for this login.

Fixes, easiest first:

- **Use Option A instead.** It does not touch your project's OAuth configuration at all.
- **Select a different project on the authenticator page** — one that has no OAuth clients yet. You can create a brand-new empty project just for logging in at <https://console.cloud.google.com/projectcreate>; it does not need to be registered for Earth Engine. The login project is only used to create the token: you still run the app with `--project <your Earth Engine project>`.
- To see whether a project already has OAuth clients, open *APIs & Services → Credentials* (<https://console.cloud.google.com/apis/credentials>) and look at *OAuth 2.0 Client IDs*.

### Option C — service account key

A service account is a robot account with its own key file; no browser login is needed.

1. In the Cloud Console of your Earth Engine project, open *IAM & Admin → Service Accounts* (<https://console.cloud.google.com/iam-admin/serviceaccounts>) → **Create service account**. Give it a name, e.g. `eetsviewer`.
2. Grant it these roles (either in the wizard or later under *IAM & Admin → IAM*):
   - **Earth Engine Resource Viewer** (`roles/earthengine.viewer`)
   - **Service Usage Consumer** (`roles/serviceusage.serviceUsageConsumer`)
3. Open the service account → **Keys** → **Add key** → **Create new key** → **JSON**. A `.json` file downloads. Keep it private; anyone with it can use your project.
4. Pass the key to the app (the project is taken from the key, `--project` is optional):

```bash
docker run --rm -t \
  -v /path/to/key.json:/key.json:ro \
  -p 2742:2742 \
  vudongpham/eetsviewer eetsviewer --key /key.json
```

> Some organisations disable creating service account keys (policy `iam.disableServiceAccountKeyCreation`). Use Option A in that case.

---

## Step 3 — Get the app

With Docker (any OS):

```bash
docker pull vudongpham/eetsviewer:latest
```

Or with pip (Python 3.11 or newer):

```bash
pip install git+https://github.com/vudongpham/ee-ts-viewer.git
```

---

## Step 4 — Run

With Docker, replace `ee-yourname` by your project ID:

```bash
docker run --rm -t \
  -v ~/.config/earthengine:/root/.config/earthengine \
  -p 2742:2742 \
  vudongpham/eetsviewer eetsviewer --project ee-yourname
```

With pip:

```bash
eetsviewer --project ee-yourname
```

Open <http://localhost:2742> in a web browser. See the [README](README.md#options) for all options (port, workers, …).

### Windows

`~` is not expanded in `cmd.exe`. Use the full path to your user folder, e.g. in PowerShell:

```powershell
docker run --rm -t -v ${env:USERPROFILE}\.config\earthengine:/root/.config/earthengine -p 2742:2742 vudongpham/eetsviewer eetsviewer --project ee-yourname
```

If you logged in with Option A on Windows, the credentials are in `%USERPROFILE%\.config\earthengine\credentials`.

---

## Troubleshooting

| Message | Cause and fix |
|---|---|
| `Project has an incompatible OAuth2 Client configuration` | The project chosen in the Notebook Authenticator already has an OAuth client. Use [Option A](#option-a--log-in-on-your-own-computer-recommended) or pick another project — see [above](#project-has-an-incompatible-oauth2-client-configuration). |
| `Please authorize access to your Earth Engine account` | No credentials in `~/.config/earthengine`. Do [Step 2](#step-2--log-in-once), and check that the `-v` path in `docker run` points to the folder containing the `credentials` file. |
| `A Google Cloud project with the Earth Engine API enabled is required` | Pass `--project ee-yourname` (or set `EE_PROJECT`). |
| `Earth Engine API has not been used in project … or it is disabled` | Enable the API on that project (link in [Step 1](#step-1--create-and-register-a-cloud-project)), wait a minute and try again. |
| `Not signed up for Earth Engine` / `project is not registered` | Register the project at <https://code.earthengine.google.com/register>. |
| `Caller does not have required permission to use project` / `PERMISSION_DENIED` | The account (or service account) needs a role on the project: *Earth Engine Resource Viewer* (or Writer) and *Service Usage Consumer*. Ask the project owner to add it under *IAM & Admin → IAM*. |
| `redirect_uri_mismatch` or the browser cannot reach `localhost:8085` | You ran `--auth_mode=localhost` where the browser is on another machine (SSH, Docker). Use `--auth_mode=notebook` or `gcloud`, or log in on your own computer and copy the `credentials` file. |
| `Too many concurrent aggregations` | Lower the number of parallel requests, e.g. `-w 4`. |
| The page does not open | Make sure `-p 2742:2742` is in the `docker run` command and nothing else uses that port; otherwise use another host port (see the README). |
