    # wMaktab - Electronic School Platform

wMaktab is a complete, working school platform with original branding: login, student / teacher / administrator dashboards, grades by subject, quarterly and yearly statistics, attendance, weekly timetable, homework, school news, user management and role-based permissions.

It is **not** a mockup. Everything is stored in SQLite and every button calls the Flask API.

| Layer | Technology |
|---|---|
| Pages | HTML5 (Jinja templates), CSS3, vanilla JavaScript |
| Server | Python 3 + Flask (`main.py`, `auth.py`, `database.py`) |
| Database | SQLite (file `instance/wmaktab.db`, created automatically) |
| Statistics | C++17 (`statistics.cpp`), compiled automatically and called by Flask |

> All demo people and data are fictional. wMaktab does not use eMaktab credentials, branding or student data.

## Quick start

Requirements: **Python 3.9+**. A C++ compiler (`g++`, `clang++` or `c++`) is recommended but optional.

```bash
# 1. open the project folder
cd wmaktab

# 2. (recommended) create a virtual environment
python3 -m venv venv
source venv/bin/activate            # Windows: venv\Scripts\activate

# 3. install the only dependency (Flask)
pip install -r requirements.txt

# 4. run
python main.py
```

Open **http://127.0.0.1:5000** in your browser.

On first start the app creates the database, fills it with demo data and compiles the C++ statistics engine into `bin/`. Nothing else needs to be done.

### Demo accounts

| Role | Username | Password |
|---|---|---|
| Student | `student` | `student123` |
| Teacher | `teacher` | `teacher123` |
| Administrator | `admin` | `admin123` |

The login page also has one-click buttons that fill these in. Other demo students use `student123` (for example `malika.yusupova`), other teachers use `teacher123` (for example `t.english`).

## C++ statistics engine

`statistics.cpp` reads `key value` lines from standard input and prints JSON with count, mean, median, min, max, standard deviation, mode, rounded mark (1-5), quality rate (share of 4-5), success rate (share of 3-5) and the grade distribution.

Flask (`stats_bridge.py`) compiles it on first use and pipes the grades from SQLite through it every time a page needs statistics, so numbers update automatically when a grade is saved.

Manual build and test:

```bash
g++ -O2 -std=c++17 -o bin/wmaktab_stats statistics.cpp
printf 'math 5\nmath 4\nmath 3\n' | ./bin/wmaktab_stats
```

If no compiler is installed, an equivalent pure-Python implementation is used automatically, so the site still works. The footer of the Statistics page tells you which engine is active, and `GET /api/health` reports it too.

### How marks are calculated

* **Quarter mark** = average of that quarter's grades, rounded half up (3.5 becomes 4).
* **Year mark** = average of the quarter marks, rounded.
* **Quality rate** = share of grades that are 4 or 5. **Success rate** = share that are 3 or higher.
* **Attendance rate** = days present or late, divided by all marked days.
* School quarters: Sep-Oct = 1, Nov-Dec = 2, Jan-Mar = 3, Apr-Aug = 4. A grade's quarter is set from its date.

## What each role can do

| Feature | Student | Teacher | Administrator |
|---|---|---|---|
| Log in, change own password, edit own contact details | yes | yes | yes |
| See own grades, marks, statistics, attendance | yes | - | - |
| Enter / edit / delete grades | - | own subjects and classes | any |
| Mark class attendance | - | own classes | any class |
| View weekly timetable | own class | own schedule or any class | any class |
| Edit timetable (with teacher clash detection) | - | - | yes |
| Homework: see and tick off | yes | - | - |
| Homework: create / edit / delete / see progress | - | own | any |
| News: read | yes | yes | yes |
| News: post | - | to students or everyone | to anyone, can pin |
| Class statistics and student ranking | - | own classes | any class |
| School-wide statistics | - | - | yes |
| User management (create, edit, disable, delete, reset password) | - | - | yes |
| Classes, subjects and teaching assignments | - | - | yes |

Permissions are enforced on the server for every API call (the menus only reflect them).

## Project structure

```
wmaktab/
├── main.py            Flask app: pages + JSON API + business rules
├── auth.py            login, sessions, CSRF, lock-out, role decorators
├── database.py        SQLite schema, helpers and demo-data seeder
├── stats_bridge.py    compiles and calls the C++ engine (Python fallback)
├── statistics.cpp     C++ statistics engine
├── requirements.txt   Flask
├── README.md
├── templates/
│   ├── index.html     public landing page
│   ├── login.html     login page
│   └── dashboard.html application shell (single-page app)
├── static/
│   ├── style.css      design system (blue and white, responsive)
│   ├── script.js      front end: API client, views, charts, modals
│   └── favicon.svg
├── bin/               compiled C++ engine (generated)
└── instance/          SQLite database + secret key (generated)
```

## Configuration

| Environment variable | Meaning | Default |
|---|---|---|
| `PORT` | port to listen on | `5000` |
| `HOST` | address to listen on | `127.0.0.1` |
| `WMAKTAB_DB` | path of the SQLite file | `instance/wmaktab.db` |
| `WMAKTAB_SECRET_KEY` | session signing key | generated into `instance/secret.key` |
| `WMAKTAB_DEBUG` | set to `1` for Flask debug mode | off |

Reset the demo data at any time (this deletes all changes):

```bash
python database.py --reset
```

## Security notes

* Passwords are stored as salted hashes (never in plain text).
* Five failed logins for the same username and address lock that login for 60 seconds.
* Every state-changing API call needs a CSRF token; sessions use HttpOnly, SameSite cookies.
* All user text is escaped before display, and a strict Content-Security-Policy is sent.
* Database access uses parameterised queries only.

**Before real use:** change or delete the demo accounts (Users page), set `WMAKTAB_SECRET_KEY`, and run behind a production server and HTTPS (for example `gunicorn main:app` behind a reverse proxy) instead of the built-in development server.

## Troubleshooting

* **"Statistics: Python fallback"** - no C++ compiler was found. Install `g++` (Linux: `sudo apt install g++`, macOS: `xcode-select --install`, Windows: MSYS2/MinGW) and restart, or ignore it; results are identical.
* **Port already in use** - start with another port: `PORT=5050 python main.py`.
* **Locked out of a demo account** - wait 60 seconds, or restart the server.
* **Want a clean database** - `python database.py --reset`.
