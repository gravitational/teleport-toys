# Connect Clean Room to your database

By default, Clean Room copies a sample CSV of fake employee data into each Beam. This guide switches it to read from a real Postgres database through Teleport instead.

With a database, nothing about the clean room changes except where the data comes from:

- The agent's Beam queries the database **through Teleport**, from inside the Beam. The rows go straight into the Beam and never touch the machine running the app.
- The Beam reads through a **view with no names, emails or IDs**, as a **read-only** database user.
- The Beam gets **only the roles you choose**, not all of yours.
- Every query is recorded in Teleport's **audit log**, under both your user and the Beam's own identity.
- When the run ends, the Beam is deleted and the data goes with it.

> **Status:** the database path follows the [Beams Database Demo](https://goteleport.com/docs/beams/databases/) and Teleport's documented role options, but hasn't yet been tested end to end with this app. If you hit a problem, see [Troubleshooting](#troubleshooting).

---

## What you need

- A Teleport cluster with Beams enabled, and `tsh` **18.11.9 or newer** ([download](https://goteleport.com/download/client-tools/)).
- Permission to run `tctl` (to create a role and update a user).
- A Postgres database you can enroll with Teleport. For a quick trial, the Beams Database Demo walks through running one locally in Docker.
- This repository, set up per the main [README](README.md).

---

## Step 1. Enroll your database with Teleport

Follow **Step 1** of the [Beams Database Demo](https://goteleport.com/docs/beams/databases/) to start Postgres and the Teleport Database Service. Two things to choose as you go:

- **A name** for the database in Teleport, for example `hr-demo-db`. You'll use it in Step 4.
- **A label** that marks it as sensitive HR data, for example `env=dummy-hr`. The role in Step 3 matches on this label.

Check that it's enrolled:

```bash
tsh db ls
```

You should see your database name and label in the list.

---

## Step 2. Load the data and the no-names view

`database/people_seed.sql` creates everything Clean Room expects:

| Object | What it is |
|---|---|
| `people` table | The raw data, **including** names, emails and other sensitive columns. Fake sample data for about 300 employees. |
| `v_people_coded` view | The only thing the agent can read. No names, emails, SSNs or bank details. People and managers appear as random-looking codes. |
| `readonly_analyst` | A read-only Postgres user with access to the view and nothing else. |

Create the database and user, then run the script as a database admin. If you followed the Database Demo, run these on the machine hosting the Postgres container:

```bash
docker exec postgres psql -U postgres -c "CREATE DATABASE hr_demo"
docker exec postgres psql -U postgres -c "CREATE ROLE readonly_analyst LOGIN"
docker exec postgres psql -U postgres -d hr_demo -c "GRANT CONNECT ON DATABASE hr_demo TO readonly_analyst"
docker exec -i postgres psql -U postgres -d hr_demo < database/people_seed.sql
```

**Using your own HR data?** Skip the sample table, and create a view named `v_people_coded` over your own tables with the same columns. The column list and meanings are in [`worker/playbook.md`](worker/playbook.md), section 3. Leave out anything that identifies a person: names, emails, government IDs, bank details. Then grant `SELECT` on the view, and only the view, to `readonly_analyst`.

---

## Step 3. Create a read-only Teleport role

`database/clean-room-reader.yaml` defines a role that lets a Beam use your database **only** as `readonly_analyst`, and only in `hr_demo`:

```yaml
version: v8
kind: role
metadata:
  name: clean-room-reader
spec:
  allow:
    db_labels:
      env: dummy-hr        # change to your database's label from Step 1
    db_users:
      - readonly_analyst
    db_names:
      - hr_demo
```

Edit the label if yours is different, then create the role:

```bash
tctl create -f database/clean-room-reader.yaml
```

Give the role to the person who will run Clean Room. If you use access lists or SSO, add it the way you normally grant roles. Otherwise:

```bash
tctl users update <username> --set-roles=<their current roles>,clean-room-reader
```

`--set-roles` replaces the user's whole role list, so include their existing roles (`tctl get users/<username>` shows them). Log out and back in (`tsh logout`, then `tsh login`) so the new role takes effect.

---

## Step 4. Check access from a Beam

Start a Beam that gets **only** the two roles it needs:

```bash
tsh beams add --roles=beam-user,clean-room-reader
```

Inside the Beam:

```bash
tsh db ls
tsh db connect hr-demo-db --db-user=readonly_analyst --db-name=hr_demo
```

At the `hr_demo=>` prompt, try:

```sql
SELECT org, count(*) FROM v_people_coded GROUP BY org;   -- works
SELECT full_name FROM people LIMIT 1;                     -- fails: permission denied
```

The second query failing is the point: the agent can read the coded view, but not the raw table with names. Type `\q` to leave, then `exit`, then remove the test Beam with `tsh beams rm <beam-name>`.

---

## Step 5. Run Clean Room in database mode

From the repository folder, with your Python environment active:

```bash
export CLEAN_ROOM_DATA_SOURCE=database
export CLEAN_ROOM_DB=hr-demo-db                 # name from `tsh db ls`
export CLEAN_ROOM_DB_USER=readonly_analyst
export CLEAN_ROOM_DB_NAME=hr_demo
export CLEAN_ROOM_BEAM_ROLES=beam-user,clean-room-reader
export CLEAN_ROOM_PHASE=clean-room-worker          # the worker image (see README)
python3 app.py
```

Open http://localhost:8080 and click **Run Org Health Dashboard**. The status feed now says it's pulling the view through Teleport, and the run receipt records the database, the view, the number of rows and a fingerprint of the data, instead of a CSV file.

| Setting | Default | Meaning |
|---|---|---|
| `CLEAN_ROOM_DATA_SOURCE` | `csv` | `csv` copies the sample file in; `database` pulls from your database |
| `CLEAN_ROOM_DB` | `hr-demo-db` | The database's name in Teleport |
| `CLEAN_ROOM_DB_USER` | `readonly_analyst` | The read-only Postgres user |
| `CLEAN_ROOM_DB_NAME` | `hr_demo` | The database inside Postgres |
| `CLEAN_ROOM_DB_VIEW` | `v_people_coded` | The no-names view to read |
| `CLEAN_ROOM_BEAM_ROLES` | *(empty)* | Roles the worker Beam gets. Empty means **all** of your roles, so set this. |

The **naive report** and **Customer ARR** buttons still use their sample CSV files. Database mode works the same with or without the worker image: the data is always pulled fresh inside each Beam, never stored in the image.

---

## Troubleshooting

**`tsh db ls` is empty inside the Beam, but the web UI lists the database.**
The Beam doesn't have a role that grants the database. Check that `clean-room-reader` is assigned to your user, that its label matches your database's label, and that you logged in again afterwards. Remember that a Beam only gets the roles listed in `--roles`.

**"Connection refused" on `localhost`.**
That message usually hides the real error. Run the same command with debug output:

```bash
tsh -d db connect hr-demo-db --db-user=readonly_analyst --db-name=hr_demo
```

and look near the end for lines containing `ERRO`, `denied` or `reissue`.

**`identity is not allowed to reissue certificates`.**
The Beam's identity couldn't get the database credential it needs. Check that your `tsh` and cluster meet the version requirement above. If it persists, use the default CSV mode and raise it with your Teleport contact.

**"database pull returned no data" in the app.**
The query ran but nothing was saved. Run the Step 4 checks inside a Beam started with the same `--roles` as `CLEAN_ROOM_BEAM_ROLES`.

---

## Good to know

- **The Beam can still reach the internet.** Teleport plans to restrict outbound networking by default in a future release. Until then, the privacy gate controls what the app releases, but can't stop an agent from sending data out on its own.
- **Beams expire after 11 hours.** Clean Room deletes each worker Beam as soon as its run finishes.
- **Keep sensitive columns out of the view.** The view is your main line of defense. The agent can only leak what it can read.
