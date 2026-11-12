# Butter

A small, safe command line for your Android phone. One light app, readable commands, and questions before anything dangerous happens.

```
new file notes.txt to Documents
copy photo.jpg to Pictures
unzip archive.zip
macro tidy = del file a.txt | del dir tmp
```

## Why Butter

- **Safe by default.** System folders are protected, risky commands ask `y/n` first, scripts must be trusted by content hash, and `!dry` shows what a command would do without doing it.
- **Commands you can read.** `new file a.txt to Pictures`, `!chk find file "*.txt" then echo found`. One name for each action, the same on every phone.
- **Light.** One small app. Extra tools (git, ssh, an ARM64 assembly emulator, a tiny web server) are installed only when you want them, with `tul`.
- **Made to learn from.** Clear error messages with a hint, and an assembly emulator that cannot crash the app.

Butter is not a full Linux system: there is no real shell, no `apt`, no compilers. If you need those, use Termux.

## Install

### From GitHub releases

Download the latest `.apk` from the **Releases** page and open it. Android will ask you to allow installing from your browser or file manager.

### From F-Droid

Butter is **not in F-Droid yet**: the metadata for the main F-Droid repository is prepared (see [Publishing on F-Droid](#publishing-on-f-droid)). An inclusion request for the [IzzyOnDroid](https://apt.izzysoft.de/fdroid/) repository has been sent. Until then, use the APK from Releases or build it yourself.

### Build from source

You need a Linux machine (Ubuntu in a virtual machine works well), Python 3, a JDK, and [Buildozer](https://buildozer.readthedocs.io/en/latest/installation.html) with the system packages its documentation lists (git, zip, unzip, JDK, autoconf, libtool, pkg-config, zlib, ncurses, cmake, libffi, libssl).

```sh
git clone https://github.com/resolik/butter.git
cd butter
pip install --user buildozer Cython
buildozer android debug
```

The APK appears in `bin/` (for example `bin/butter-0.1-arm64-v8a-debug.apk`). The first build downloads the Android SDK and NDK and takes a long time; later builds are fast. Everything the build uses is set in `buildozer.spec`: python-for-android `v2024.01.21`, Android API 33, NDK `25b`, one architecture (`arm64-v8a`; almost all phones are 64-bit).

**A release build** (for publishing) needs your own signing key:

```sh
buildozer android release                      # makes an unsigned APK in bin/
keytool -genkeypair -v -keystore butter.jks -alias butter -keyalg RSA -keysize 4096 -validity 10000   # once
apksigner sign --ks butter.jks bin/butter-0.1-arm64-v8a-release-unsigned.apk
```

Keep `butter.jks` and its password safe and never commit them. Every later release must be signed with the same key, or Android will refuse to update the app.

**The SSH tool** needs the Java library JSch in the build. Uncomment the `android.gradle_dependencies` line in `buildozer.spec` before building.

## First start

1. Open Butter. It asks for **all files access** (Android 11+): turn the switch on, then return to the app.
2. Your project folder is `/sdcard/butter`. Type `help` for the list of commands.
3. Type `report` if something is wrong or you have an idea.

## A quick tour

```
here                                   where am I
list                                   what is in this folder
new dir Photos                         make a folder
new file notes.txt to Photos           make a file in another folder
copy notes.txt to Photos               copy and paste in one step
zip dir Photos                         make Photos.zip
unzip Photos.zip                       unzip into a new folder named Photos
unzip Photos.zip to here               unzip straight into this folder
download https://example.com/a.zip to Download
!dry del dir Photos                    preview: nothing is deleted
!chk find file "*.txt" then echo found run a command only if a check passes
macro tidy = del file a.txt | del dir tmp
run tidy                               run a macro (arguments: $1 .. $9 and $*)
!prl download https://example.com/a.zip | echo started    run in the background
```

## Safety

- **Protected places.** Deleting, renaming and editing are refused in the storage root and inside the system folders: `Android`, `DCIM`, `Pictures`, `Movies`, `Music`, `Documents`, `Alarms`, `Notifications`, `Podcasts`, `Ringtones`. Making new files and folders, and copying into them, is allowed. The `Download` folder itself is protected, but the files inside it are free.
- Deleting, overwriting and similar actions ask `y/n`. `!y` answers yes for you; `!dry` previews.
- Scripts started with `py` or a tool run with full power, so Butter asks first and remembers your answer only for **that exact file content**: if the file changes, it asks again. `trust add script.py` / `trust remove script.py` / `trust list` manage this.
- Secrets: `git` keeps a GitHub token next to `gitkit.py` (`github_token.txt`). Never commit it. The `ssh` tool uses keys only and keeps them in the app's private folder.

## Commands

Type `help` in the app for the same list, and `help NAME` for one command. A command can be chained with `|`; the chain stops at the first error.

### Make

| Command | What it does |
|---|---|
| `new file\|dir name` | create a file or a folder |
| `download link [name]` | download a file (https) |
| `zip file\|dir name [zip]` | zip a file or a folder |
| `unzip archive` | unzip into a new folder named like the archive |
| `unzip archive to FOLDER` | unzip straight into FOLDER (to here = the folder you are in) |

### Change

| Command | What it does |
|---|---|
| `append name "text"` | append a line to a file |
| `overwrite name "text"` | overwrite a text file |
| `overwrite name "a" replace to "b"` | replace text inside a file |
| `rename file\|dir old new` | rename a file or a folder |
| `del file\|dir name` | delete a file or a folder |

### Buffer

| Command | What it does |
|---|---|
| `copy name ...` | copy to the buffer |
| `cut name ...` | cut (moves on paste) |
| `paste` | paste here |
| `buffer` | show the buffer |

### Look

| Command | What it does |
|---|---|
| `list [path]` | list a folder |
| `view path` | view a text file |
| `view path h 20` | first 20 lines |
| `view path t 20` | last 20 lines |
| `view path 120 140` | lines 120 to 140 |
| `info file\|dir [path]` | info about a file or a folder |
| `grep "text" [path]` | search text in files |
| `find file\|dir pattern [path]` | find files or folders by name |

### Walk

| Command | What it does |
|---|---|
| `go to [path]` | go to a folder (no path = back to butter) |
| `go up` | go up one folder |
| `here` | where am I (the current folder) |

### Modifiers

| Command | What it does |
|---|---|
| `!ma cmd` | keep going after errors |
| `!hl cmd` | hide the logs of cmd |
| `!dry cmd` | show, do not do |
| `!y cmd` | say yes to its question |
| `!prl cmd` | run cmd in the background, go on at once (max 4) |
| `!chk A then B` | run B if A works |
| `!chk A else B` | run B if A fails |
| `macro NAME = cmds` | save commands under a name (use \| between them) |
| `macro NAME` | type the commands one per line, finish with end (cancel throws it away) |
| `macro list\|show\|del` | list your macros / show one / delete one (macro show NAME) |
| `run NAME [args]` | run a macro; $1 .. $9 and $* inside it are your arguments |
| `to FOLDER` | after a command: do it in that folder (new file a.txt to Pictures; download URL to Music; paste to Docs; copy a.txt to Docs; unzip a.zip to Games) |

### Other

| Command | What it does |
|---|---|
| `report` | how to report a problem: contacts and one line of info to copy |
| `echo text` | print text |
| `py file [args]` | run a Python script |
| `env [NAME [value]]` | variables for scripts (tokens): env, env NAME value, env -d NAME |
| `edit file "old" "new"` | edit one exact piece of text; refuses if it is not unique (all, -e: \n) |
| `pip command` | install libraries: pip install rich, pip list ... (runs hpip.py) |
| `git command` | GitHub from the phone: git clone OWNER/NAME, status, pull, push, log, ls (runs gitkit.py; tul install gitkit) |
| `ssh ...` | SSH with keys, no passwords: ssh key new, ssh add pi pi@host, ssh trust pi, ssh pi uptime (runs sshkit.py; tul install ssh) |
| `net command` | ip, dns, port, ping, trace: net ip, net dns NAME ... (runs net.py; tul install network) |
| `sv [folder] [min]` | share a folder in a browser; cancel stops it (runs sv.py; tul install network) |
| `asm prog.s` | run an ARM64 assembly program in a safe emulator (runs asm.py; tul install asm) |
| `tul command` | Butter tools: tul install network, tul updates, tul update --all (runs tul.py) |
| `wakelock on [min]` | keep the processor awake (30 min by default, 480 at most) |
| `wakelock off` | release the wakelock |
| `wakelock status` | wakelock and battery-limit status |
| `battery` | ask Android to lift battery limits for Butter |
| `trust add file` | trust a script: it runs without asking while its content stays the same |
| `trust remove file` | stop trusting a script |
| `trust list` | list trusted scripts |
| `history [N]` | last commands |
| `redo [N]` | run a past command again |
| `clear` | clear the screen |
| `debug [on\|off]` | show technical details |
| `cancel` | stop a running job |
| `help [command]` | this list |

## Tools

Tools are small scripts installed with `tul` from this repository. Each gets its own command, so you never type `py`.

| Install | Command | What it is |
|---|---|---|
| `tul install hpip` | `pip ...` | Install pure Python libraries from PyPI |
| `tul install gitkit` | `git ...` | GitHub from the phone: clone, status, pull, push |
| `tul install network` | `net ...`, `sv ...` | ip, dns, port, ping, trace; share a folder in a browser |
| `tul install asm` | `asm prog.s` | Run ARM64 (AArch64) assembly in a safe emulator |
| `tul install ssh` | `ssh ...` | Run commands and copy files over SSH, with keys only (needs the JSch library in the build, see `buildozer.spec`) |

`tul update --all` updates installed tools. After installing a tool, `trust add TOOL.py` stops the question on every run.

## Reporting problems

Type `report` in the app: it prints where to write and one line with your versions (no file names, commands or tokens). You can also open an issue on GitHub.

## Publishing on F-Droid

Status: the metadata file is prepared (`metadata/org.butter.butter.yml` for the fdroiddata repository); the merge request is **not submitted yet**. This section is the checklist.

What F-Droid requires (see the [F-Droid Quick Start Guide](https://f-droid.org/docs/Submitting_to_F-Droid_Quick_Start_Guide/) and the [Inclusion Policy](https://f-droid.org/docs/Inclusion_Policy/)):

1. **A public source repository with a free software license file.** Done: `LICENSE` (MIT). Only free dependencies and free build tools.
2. **A tag for every release**, for example `v0.1`.
3. **Store text and pictures in the repository**, in the Fastlane layout:
   - `fastlane/metadata/android/en-US/short_description.txt` (under 80 characters, no trailing dot)
   - `fastlane/metadata/android/en-US/full_description.txt`
   - `fastlane/metadata/android/en-US/images/icon.png` (512x512)
   - `fastlane/metadata/android/en-US/images/phoneScreenshots/1.png`, `2.png`, ...
   - `fastlane/metadata/android/en-US/changelogs/<versionCode>.txt` (up to 500 characters)
4. **A merge request to [fdroiddata](https://gitlab.com/fdroid/fdroiddata)** adding `metadata/<applicationId>.yml`: descriptive fields (license, web site, source code, issue tracker), the repository, a `Builds` block, and auto-update settings. Install `fdroidserver` and run `fdroid lint` and `fdroid build` locally first.

Things specific to Butter:

- **Application ID** is `package.domain` + `package.name` from `buildozer.spec`: `org.butter.butter`. It cannot be changed after release.
- **Build.** F-Droid builds from source on its own server. Buildozer / python-for-android download the SDK, NDK and Python sources while building, so the metadata will need custom `sudo`, `prebuild` and `build` steps. Look at other Python / Kivy apps in fdroiddata for examples, and ask the F-Droid maintainers early.
- **Permissions.** The app uses `MANAGE_EXTERNAL_STORAGE` (all files access); explain why in `full_description.txt`.
- **Downloaded tools.** `tul` downloads small tools from this repository and `pip` installs libraries from PyPI. The tools are free software from this repository and are checked against the hashes in `tools.json`; be ready to explain this to the reviewers.
- **Release routine.** Raise `version` in `buildozer.spec`, add `fastlane/metadata/android/en-US/changelogs/<versionCode>.txt`, commit, tag (`git tag v0.2 && git push --tags`), then publish a GitHub release with the signed APK attached (this is also what IzzyOnDroid reads).

**Faster alternative: IzzyOnDroid.** It is a separate F-Droid style repository that takes the APK you publish. Requirements: an OSI/FSF-approved license, public code, and the `.apk` attached to a tagged GitHub release (about 30 MB per app is the rule of thumb). Request inclusion through the issue templates in their repository. Once users can add that repository to the F-Droid client, Butter can be installed and updated from there.

## License

MIT, see [LICENSE](LICENSE). Butter is free software: use it, change it, share it.
