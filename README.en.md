# Portable Local AI Workbench

A local AI workbench that runs straight off a removable drive. The model and all the
programs live on the drive: nothing is installed into the system, nothing is written
to the registry, and unplugging the drive leaves no trace.

**English** | [中文](README.md)

> Note: this document is in English, but the **web interface itself is in Chinese**.
> The UI has not been translated yet.

---

## 1. How to use it

1. Plug the drive into a Windows machine (a USB 3.0 port is faster).
2. Open the `portable-ai` folder and **double-click `启动.bat`** (the launcher).
3. A console window opens, starts the model server, and then opens your browser.
   - **The first start is slow** (it unpacks the program and reads a multi-GB model),
     so give it time and do not close the window.
   - That window is the on/off switch: **close it when you are done** and the model
     server shuts down with it.
4. The browser shows four entries:

| Entry | What it does |
|---|---|
| **Chat** | Talk to the local model and write; text streams out character by character |
| **Model Manager** | See which models exist and which one is loaded; switch with one click (takes 1-4 minutes) |
| **Novel** | Create a book from a template and edit outline and text in the page |
| **System Check** | CPU / memory / GPU / disk in one click, ending in a plain-language conclusion |

> Everything is offline. Only the very first model download needs a network, and your
> conversations never leave the machine.

### You can also just say what you want

The box at the bottom of the home page takes plain language:

```
is the computer running slow?
which model is loaded right now?
what models can I pick from?
```

**The flow is "translate, show you, you confirm, then run"** - the model does not get
to act on its own:

1. The local model turns your sentence into "which function to call, with what arguments";
2. The page shows you the result (for example: it wants to run `modelctl` `switch` `some-model.gguf`);
3. Only when you click "Confirm" does anything actually happen. "Cancel" does nothing.

This is because a small local model sometimes misreads a request. **Arguments are
validated again on the server** (the skill must be registered, the model file must
really exist, no paths allowed), so the worst case is a wrong translation - never a
stray file operation.

### System prompt and thinking mode

- **The system prompt is empty by default.** The program does not inject any persona
  for you, so the model simply answers in its own way. To give it one (for example
  "you are a wuxia novelist"), open **Settings** on the Chat page and type it in.
- **Thinking mode is off by default.** Some newer models like to think out loud at
  length before answering, which means waiting longer and burning tokens for nothing
  when you are writing. Turning it off makes the program quietly inject `/no_think`
  (measured to work; the `chat_template_kwargs` approach does nothing on this build of
  koboldcpp). To let the model think first, turn the switch on in Settings and raise
  the maximum reply length.

### Optional launch settings

Before double-clicking `启动.bat`, you can `set` these in the same window:

```bat
set PORTABLE_AI_GPULAYERS=12      :: GPU layers; fewer is safer
set PORTABLE_AI_BACKEND=vulkan    :: use the other backend (default: cuda)
set PORTABLE_AI_MODEL=other.gguf  :: pick which model to load
set PORTABLE_AI_MAXWAIT=600       :: overall cap for model loading, in seconds (default 300)
启动.bat
```

If you would rather not type commands, edit the defaults at the top of `启动.bat`.

### How long does startup take?

**Fastest route first: leave that console window open.**

| Situation | Time until usable |
|---|---|
| Model server already running (window still open) | **about 1.4 seconds** |
| Cold start (window was closed) | **about 126 seconds** |

Close browser tabs freely - the model stays in memory. The next time you double-click
`启动.bat` you will see "model server already running on port 5001, reusing it" and you
are in within seconds. Only close the window when you are done for the day.

A cold start spends its time on three things:

1. **Unpacking** the runtime bundled inside the model server (about 600 MB - the price
   of a single-file executable, roughly 32 seconds);
2. **Reading the model** (a couple of GB; a USB drive measured 40-97 MB/s, so 40-60 seconds);
3. **Uploading weights to VRAM and building the KV cache.**

> The only real speedups are hardware: **more RAM** (enough to hold the whole model file
> so the OS can cache it - the second load is then nearly instant; cache hits measured
> about 20x faster than reading the drive), or **putting the model on an internal SSD**
> (a couple of seconds to read, but you lose the "unplug and go" property).

The startup messages are designed around this:

- At **120 seconds** it tells you plainly: `waited 120s, still reading the model, continuing...`
- Then it **keeps waiting** until **300 seconds** (`PORTABLE_AI_MAXWAIT`) before giving up;
- If the process dies on its own, it gives up immediately instead of waiting pointlessly;
- Only after a real failure does it try the other backend - and on a slow drive the other
  backend **is not faster**, so waiting is much better than restarting.

> Why there is no "stuck detection": measuring CPU was useless (during loading the CPU
> sat still - measured flat at 15.4s of CPU time from second 30 to second 105), and
> watching memory/VRAM changes also misfired, killing healthy loads. So it just waits,
> simply and reliably.

---

## 2. What is in here

```
portable-ai\
  bin\            koboldcpp.exe (CUDA build), koboldcpp_nocuda.exe (Vulkan/CPU build)
  models\         gguf models
  py\             portable Python 3.12 (standard library only, no third-party packages)
  app\
    workbench.py  backend (Python standard-library http.server)
    launcher.py   find/start/wait-for/stop the model server (shared by launcher and skills)
    web\          index.html  chat.html  status.html  model.html  novel.html
    skills\       status.py (system check), modelctl.py (model manager),
                  switcher.py (background model switch), router.py (plain-language
                  translation), skills.json
  novels\         your books; 模板\ holds the templates
  work\           the only place a skill may read and write (switch state, speed cache)
  _tools\         start.ps1 (launcher logic), install.ps1 (fetch large files), put.ps1
  启动.bat        double-click this (the name means "start")
  README.md       this file
```

> `bin\` `models\` `py\` are not in git (large files that can be downloaded again,
> about 3 GB in total). After cloning on another machine, run this once:
> ```
> powershell -ExecutionPolicy Bypass -File _tools\install.ps1
> ```
> It brings back **portable Python and the model server** (existing files are skipped).
> **No model is preset** - pick one of these two routes:
> ```
> # 1) let the script download one (give it a URL)
> powershell -ExecutionPolicy Bypass -File _tools\install.ps1 `
>     -ModelUrl "https://hf-mirror.com/<repo>/resolve/main/<file>.gguf"
>
> # 2) download any .gguf yourself and drop it into models\
> ```

---

## 3. Adding models

1. Get a **gguf** model file (ends with `.gguf`) and put it in the `models\` folder.
2. Go to **Model Manager** and click to switch, or restart `启动.bat`.

The program picks the **first by name** among the gguf files in `models\` as the default.
To choose explicitly, use `PORTABLE_AI_MODEL` as shown above.

**FAT32 has a 4 GB per-file limit.** If the drive is FAT32, a single file cannot exceed
4 GB. A larger model simply cannot be copied over (you get "not enough space", which is
really the format limit, not free space).

**VRAM matters.** How many layers to put on the GPU (`--gpulayers`) depends on **your**
card. The default is **22 layers**, which measured about 2.5 GB of VRAM on a 4 GB card,
leaving a safety margin.

> A word of warning: **do not just crank `--gpulayers` up.** Pushing every layer onto the
> GPU exhausted VRAM, and Windows blue-screened with stop code `0x0000010E`
> (VIDEO_MEMORY_MANAGEMENT_INTERNAL). The right way is to **add a few layers at a time**,
> watching the VRAM figure on the System Check page, and **stay under 85%**
> (85% of 4 GB is about 3481 MB - a number learned the hard way).

If a bigger model will not run, lower the layer count, or set it to `0` to run entirely
on CPU. Also note that **vision models** (a main model plus an `mmproj` projector) push
VRAM close to the ceiling and **may cross the 85% line** - weigh that yourself.

---

## 3b. Writing a novel

On the home page open **Novel** and click **New novel**, then give it a name.
It creates this from the templates:

```
novels\<book name>\
  设定卡.md             setting card: characters, world, prose style - the anchor
  单元01\大纲.md        unit outline: what happens in this unit, with a beat plan
  单元01\第01节.md      the text, one file per section (8 sections per unit)
  单元01\第02节.md
  ...
  单元02\ ...  单元03\ ...
```

(The Chinese file names mean `setting card`, `unit NN`, `outline`, `section NN`.)

The left side is a file tree; the right side is editable, `Ctrl+S` saves.

**Suggested order of work** (the templates say the same):

1. Fill in `设定卡.md` first - characters, world, and especially **prose style**. That
   section affects how good the result reads more than anything else.
2. Fill in `单元01\大纲.md` and **decide the ending of the unit first**; that keeps the
   middle from wandering off.
3. Go to **Chat**, paste in **setting card + outline + which section to write**, and ask
   for that one section only.
4. Paste the result back into `第NN节.md` and leave three or four lines of summary in the
   outline's "section summary" field.
5. When writing the next section, paste the summary along with it - the context window is
   only 8192, and without the summary the model forgets quickly.

> In one sentence: **never ask it for a whole chapter at once.** Push section by section,
> and read each one before moving on.

---

## 4. Adding skills

A "skill" is a Python script in `app\skills\` that uses **only the standard library**.

The rules are simple:

1. Put the script in `app\skills\`, for example `app\skills\mytool.py`.
2. **Only read and write inside `work\`** - never touch anything else.
3. For system information, call commands like `Get-CimInstance` or `nvidia-smi`, and
   **if any single command fails, skip that item - never let the whole script crash**.
4. Print a JSON blob to standard output using UTF-8; the workbench passes it through.
5. Add an `/api/xxx` endpoint in `app\workbench.py` to call it (copy the pattern from
   `/api/status`).

The reference implementation is `app\skills\status.py`, commented section by section.

---

## 5. Troubleshooting

### `启动.bat` flashes and disappears, or prints a pile of errors

Read the message in the window. If it says something like "cannot find py\python.exe",
the files were not copied completely - copy the whole folder again.

### "waited 120 seconds and it is still not ready" / the model server will not start

Try these in order:

1. **Close programs that use the GPU** (games, video in the browser, editors).
2. **Give the GPU fewer layers**: `set PORTABLE_AI_GPULAYERS=12`
3. **Run entirely on CPU**: `set PORTABLE_AI_GPULAYERS=0` (slow, but it will start)
4. **Switch to the other backend**: `set PORTABLE_AI_BACKEND=vulkan`
5. **Wait longer**: `set PORTABLE_AI_WAITTIMEOUT=240` (the drive is slow; 120 seconds is
   sometimes not enough)

### Switching models sits on "loading" for a long time

That is normal. A cold read of a couple of GB takes 1-4 minutes, and switching also has
to stop the old server first. The page keeps showing progress and updates on its own.
**Do not close the console window** - closing it stops the model server too.

If it is still going after 5 minutes, check the Model Manager page for an error, or look
at the last lines of `work\model_server.log`.

### It works at first, then stalls, or the whole machine feels slow

Not enough memory. The model itself needs 1-2 GB, and browsers are hungry. Close programs
you are not using. The System Check page shows how much memory is in use.

### It blue-screened

Do not panic - project files survive a reboot. Check the System Check page and the crash log:

- If it crashed **while loading a model**, VRAM was almost certainly exhausted. See
  section 3 and lower `--gpulayers`.
- To confirm, look for a dump file (usually under `Windows\Minidump\` on the system drive;
  reading it needs administrator rights). Most of the time you do not need this - just
  lower `--gpulayers` as above.

### There is still a koboldcpp running after closing the window

Normally closing the window takes it down too. If something is left over, press
`Ctrl+Shift+Esc` for Task Manager and end `koboldcpp`. It writes nothing to the registry,
so nothing is left behind except VRAM being freed.

### The page will not open ("cannot connect")

Look for that console window - is it still there? If it is, port 8000 may be taken by
another program. Use another machine, or close whatever is holding port 8000.

---

## 6. Status

**Phase 1** (startup -> chat -> system check):

- Portable Python, the model server, a model, `启动.bat`, and the workbench backend
- Chat page (streaming), System Check page, `status.py`

**Phase 2** (done):

- **Model Manager really switches models** - `modelctl.py` + `switcher.py`. One click
  starts the switch and the page shows "loading ..." - **no staring at a spinner**
  because the switch happens in the background and the page polls for progress every
  2 seconds.
- **The Novel page works** - two templates in `novels\模板\`, plus creating a book and
  editing files right in the page.
- **Plain-language translation** - `router.py` + `skills.json`. The box on the home page
  shows you what it understood before anything runs.

### Measurements (on a 4 GB-VRAM machine with a USB drive; for reference only)

> These numbers depend heavily on hardware. A different machine will differ -
> **do not treat them as a performance promise.**

| Item | Result |
|---|---|
| Chat speed | roughly 3-7 characters/second (22 layers on the GPU) |
| Time to first character | 1-4 seconds |
| VRAM in use | about 2.5 GB |
| Model load | **1-4 minutes** (slow cold read from a USB drive; much faster when cached) |
| Model switch, total | about 240 seconds (including one backend fallback) |
| Thinking mode off | `/no_think` measured to work; `chat_template_kwargs` does nothing here |

### Known rough edges

- The disk speed test in System Check is disturbed by a running model - on the same drive,
  34 MB/s idle versus **4 MB/s** while the model is busy. So the "SSD or HDD" conclusion is
  only accurate when measured after loading finishes; the result is cached for 7 days.
- **The model server unpacks about 600 MB of runtime at startup** (it is a PyInstaller
  single-file build). That goes into `portable-ai-extract\` under the system temp directory
  (a system SSD is usually much faster than a USB drive), and **the leftovers from the
  previous run are cleaned up before every start**, so nothing accumulates.
  > This one was learned the hard way: repeatedly force-killing the server left unpacked
  > leftovers behind, and more than 20 `_MEI` folders piled up in the system temp directory
  > - **tens of GB in total**, eating most of the system drive's free space. Cleaned up, and
  > automatic cleanup added. Unpacking to a system SSD measured nearly twice as fast as
  > unpacking to the USB drive, which is why it goes there now.
- The disk type is **guessed from measured speed**, not reported by the system. Some USB
  drives do not report a type to Windows at all.
- **The model dropdown at the top-left of the Chat page is a leftover from phase 1**:
  switch models on the **Model Manager** page instead. The dead dropdown does not affect
  chatting (messages go straight to `/api/chat`).
- The plain-language box uses the same small local model, so it **occasionally
  mistranslates**. That is why the flow is "show you first -> you confirm -> then run",
  with a final server-side validation behind it.

---

## 7. Backup (git)

This project is version-controlled with git. **The repository holds only code you wrote**;
`bin\` `models\` `py\` are excluded (they can be downloaded again, about 3 GB in total).
After cloning, run `_tools\install.ps1` once (models are not preset - supply a URL or drop
a `.gguf` in yourself; see section 2).

```powershell
# see what changed
git status
git diff

# save a version
git add -A
git commit -m "describe what changed"
```

> On a different machine or drive letter, the first `git` command may fail with
> **"dubious ownership"**. FAT32 does not record file ownership, so git refuses by default.
> Add one exception:
> ```
> git config --global --add safe.directory <project path>
> ```
> (for example `D:/portable-ai`). This is git's safety mechanism, not a broken project.

The repository identity was set locally when the repo was created (`portable-ai workbench`);
**your global git configuration was not touched** (apart from the `safe.directory` line above).