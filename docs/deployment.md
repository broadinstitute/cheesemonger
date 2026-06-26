# Deploying Cheesemonger to dev.cds.team

This guide deploys cheesemonger as a Docker container on **dev.cds.team**, serving
data from a **Persistent Disk**, fronted by the existing oauth2_proxy (Broad
OAuth). It follows the conventions already used on that host (see the
`broadinstitute/cds-ansible-configs` repo): apps run as systemd-managed Docker
containers pulled from the CDS Artifact Registry, bound to `127.0.0.1:<port>`,
and exposed by oauth2_proxy via a path prefix. `perturb-scuba` is the closest
existing analog.

## How dev.cds.team is wired (context)

```
Internet ──HTTPS:443──> oauth2_proxy (Broad OAuth, TLS) ──by path prefix──> 127.0.0.1:<port> (your container)
```

- **Image registry:** `us-central1-docker.pkg.dev/cds-docker-containers/docker/<app>`.
  Hosts pull with the vaulted SA key at `/etc/google/auth/docker-pull-creds.json`
  (set up by the `cds-docker-containers` + `docker-gcr` roles).
- **Process model:** one systemd unit per app running `docker run --rm`.
- **Persistent disks:** `/data1` and `/data2` are already mounted on dev.cds.team
  (see the `mount:` tasks under `hosts: dev.cds.team` in `site.yml`).
- **Routing/auth:** oauth2_proxy (`roles/oauth2_proxy/templates/dev.cds.team/oauth2_proxy.cfg`)
  lists `upstreams` by path prefix; everything is behind Broad OAuth unless added
  to `skip_auth_regex`.

> **Auth note:** cheesemonger itself has no authentication yet (see
> [planning.md](planning.md)). Behind oauth2_proxy it's gated at the edge to
> `@broadinstitute.org`, which covers the open ingest/delete endpoints for this
> deployment. Don't expose the container port publicly (bind to `127.0.0.1`).

We'll use:
- **Port:** `127.0.0.1:4310` → container `:8000` (perturb-scuba already uses 4310's neighbor 4300; pick any free port).
- **Path prefix:** `/cheesemonger/` → set `API_PREFIX=/cheesemonger` so the app serves under that prefix.
- **Data dir:** `/data2/cheesemonger` on the host → `/mnt/data` in the container.

---

## 1. Persistent disk for the data

You have two options.

### Option A — reuse the existing `/data2` disk (simplest)

`/data2` is already a mounted Persistent Disk. Just give cheesemonger a
subdirectory:

```bash
sudo mkdir -p /data2/cheesemonger
```

Nothing else to mount. Skip to step 2.

### Option B — a dedicated Persistent Disk

Create, attach, format, and mount a separate disk (do this once). Replace
`<zone>` / size as needed; find the instance name with `gcloud compute instances list`.

```bash
# Create and attach (run from your workstation with gcloud)
gcloud compute disks create cheesemonger-data --size=200GB --type=pd-balanced --zone=<zone>
gcloud compute instances attach-disk dev-cds-team --disk=cheesemonger-data \
  --device-name=cheesemonger-data --zone=<zone>
```

```bash
# On the VM: format ONCE (skip if it already has data), then find its UUID
DEV=/dev/disk/by-id/google-cheesemonger-data
sudo mkfs.ext4 -F "$DEV"          # ⚠ formats — only on a fresh disk
sudo blkid "$DEV"                 # note the UUID=...
```

Add the mount to `site.yml` under `hosts: dev.cds.team` (so it survives reboots
and re-provisioning), alongside the existing `/data1` / `/data2` mounts:

```yaml
    - mount:
        name: /data/cheesemonger
        src: UUID=<uuid-from-blkid>
        state: mounted
        fstype: ext4
        passno: 2
```

Then the data dir is `/data/cheesemonger` (use that wherever `/data2/cheesemonger`
appears below).

---

## 2. Add a `cheesemonger` ansible role

In a `cds-ansible-configs` checkout, create `roles/cheesemonger/` mirroring
`perturb-scuba`.

`roles/cheesemonger/templates/pull.sh`:

```bash
#!/bin/bash
GOOGLE_APPLICATION_CREDENTIALS=/etc/google/auth/docker-pull-creds.json \
  docker pull us-central1-docker.pkg.dev/cds-docker-containers/docker/cheesemonger
```

`roles/cheesemonger/templates/cheesemonger.service`:

```ini
[Unit]
Description=cheesemonger dockerized app
Requires=docker.service
After=docker.service

[Service]
Restart=always
ExecStartPre=-/usr/bin/docker rm -f cheesemonger
ExecStart=/usr/bin/docker run --rm --name cheesemonger \
  -p 127.0.0.1:4310:8000 \
  -v /data2/cheesemonger:/mnt/data \
  -v /data2/taiga/token:/root/.taiga/token:ro \
  -e DATA_DIR=/mnt/data \
  -e API_PREFIX=/cheesemonger \
  -e TAIGA_GENE_MAPPING_ID={{ cheesemonger_gene_mapping_id }} \
  -e TAIGA_TOKEN_PATH=/root/.taiga/token \
  us-central1-docker.pkg.dev/cds-docker-containers/docker/cheesemonger
ExecStop=/usr/bin/docker stop -t 2 cheesemonger

[Install]
WantedBy=default.target
```

`roles/cheesemonger/tasks/main.yml`:

```yaml
---
- name: Install pull cheesemonger script
  template: src=pull.sh dest=/usr/local/bin/cheesemonger-pull mode=u=rwx,g=rx,o=rx

- name: Pull latest cheesemonger docker image
  command: /usr/local/bin/cheesemonger-pull

- name: Ensure data dir exists
  file: path=/data2/cheesemonger state=directory

- name: Install systemd service for cheesemonger
  template: src=cheesemonger.service dest=/etc/systemd/system/cheesemonger.service

- name: enable cheesemonger service
  shell: systemctl daemon-reload && systemctl enable /etc/systemd/system/cheesemonger.service && systemctl restart cheesemonger
```

Notes:
- The Taiga token mount enables `/gene_mappings`. Set
  `cheesemonger_gene_mapping_id` (e.g. in `group_vars` or the play vars). If you
  don't need gene mappings yet, drop the two Taiga lines and the env var; the
  server starts fine without them.
- `ExecStartPre=-docker rm -f` clears any stale container after an unclean stop.

Register the role under `hosts: dev.cds.team` in `site.yml`:

```yaml
- hosts: dev.cds.team
  become: yes
  roles:
    # ... existing roles ...
    - perturb-scuba
    - cheesemonger          # <-- add
```

## 3. Expose it via oauth2_proxy

Add an upstream to `roles/oauth2_proxy/templates/dev.cds.team/oauth2_proxy.cfg`
in the `upstreams = [ ... ]` list:

```python
    "http://127.0.0.1:4310/cheesemonger/",
```

Because the upstream path is `/cheesemonger/`, oauth2_proxy forwards requests
*with* that prefix — which is why the container sets `API_PREFIX=/cheesemonger`
so its routes line up (`/cheesemonger/health`, `/cheesemonger/datasets`, …).

(Optional) to let unauthenticated health checks through, add to `skip_auth_regex`:

```python
    "\\.*/cheesemonger/health",
```

## 4. Provision

From the `cds-ansible-configs` repo (see its README for the exact wrapper):

```bash
ansible-playbook site.yml --limit dev.cds.team
```

This pulls the image, installs+starts the systemd unit, updates oauth2_proxy, and
reloads nginx. Check it:

```bash
# on the VM
systemctl status cheesemonger
docker logs cheesemonger --tail 50
curl -s http://127.0.0.1:4310/cheesemonger/health      # {"status":"ok"}
```

From your browser (through OAuth): `https://dev.cds.team/cheesemonger/health`,
and API docs at `https://dev.cds.team/cheesemonger/docs`.

## 5. Load data

The data dir starts empty. Load a block either way:

```bash
# A) on the VM, via the container's CLI (server-local or gs:// source)
docker exec cheesemonger python -m cheesemonger load \
  --source gs://cds_perturbseq_datasets/perturb-scuba/PS-SC-1_degs_broadcast.zarr \
  --dataset perturb-scuba --block PS-SC-1 --create-dataset

# B) remotely via the API / cheesypy (server reads the gs:// source)
#    cm = Cheesemonger("https://dev.cds.team/cheesemonger")
#    cm.load("perturb-scuba", "PS-SC-1",
#            "gs://cds_perturbseq_datasets/perturb-scuba/PS-SC-1_degs_broadcast.zarr",
#            create_dataset=True)
```

For `gs://` sources the VM's service account needs **Storage Object Viewer** on
the bucket. Data persists on the disk across container restarts and redeploys.

## Updating the deployment

CI builds and pushes `:latest` (and a `:<sha>` tag) on changes to server code.
To roll the running container to the latest image, re-run the pull + restart
(e.g. re-run the play, or on the VM):

```bash
/usr/local/bin/cheesemonger-pull && sudo systemctl restart cheesemonger
```

> For reproducible rollouts, prefer pinning the service to an immutable
> `:<sha>` tag instead of `:latest` and bumping it per deploy.
