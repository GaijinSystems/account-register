# Security

## Where this is meant to run

**account-register is for one person, on a network you control: your own machine, a home or
office LAN, or behind a VPN. Do not put it on the open internet.**

It was built carefully, but it has not had an outside security audit, and several of its
choices only make sense on a private network:

- **The only login is one password.** There is no second factor and no user accounts.
- **Wrong-password backoff is global,** not per address. Anyone who can reach the login page
  can keep you waiting (up to five minutes per attempt) by guessing.
- **The web server is Python's single-threaded `wsgiref` server.** It handles one request at a
  time, so a few slow connections can stall it. It needs a reverse proxy in front for HTTPS.
- **The password is the encryption key.** Whoever guesses it has everything; nothing else
  stands in the way.

## What the design protects

**The data at rest.** Every row and the account list are encrypted with AES-256-GCM under a
key derived from the password (scrypt, n=2^17, r=8, p=1). The key is never stored. Someone who
gets a copy of the data directory, a backup, the git remote or the disk, while the app is
locked or not running, can read:

- account ids (the folder names) and years
- how many rows each sheet has
- which row numbers changed and when (commit times and messages such as
  `checking/2026: edit row 2 (amount, description)`)
- the size of each entry, rounded up to 64 bytes

They cannot read amounts, dates, descriptions, notes, or account names. They can try
passwords offline at the cost of one scrypt run (about 128 MB of memory and a fraction of a
second) per guess, so **a short or common password can still be guessed.** Use a long one.

**Tampering with rows.** A changed row, or a row moved into another sheet or account, fails to
decrypt and is reported. Encryption does not detect a deleted row, reordered rows, or a file
put back to an older version. Git history records those, for anyone who looks.

**The history, from the web.** Only the recorder process touches the data and runs git. The
web process can send it a fixed list of operations and nothing else: no file paths, no git
commands. Every change is a new commit; no operation rewrites or deletes history, and an
undo is itself a new commit. Exporting and changing the password ask for the password again.

**Imported files.** Spreadsheets, CSV files and bank downloads are read only by the command
line, never by the web page, and are treated as untrusted: the readers use the Python
standard library, limit sizes, refuse XML with a DOCTYPE, and strip control characters from
anything they print. Every import shows a preview and asks before it writes.

**The browser side.** Sessions are random tokens kept in the web process's memory; the cookie
is `HttpOnly`, `SameSite=Strict` and (by default) `Secure`. Writes need a CSRF token and a
matching `Origin`. The pages load no outside resources and send a strict Content Security
Policy.

## What it does not protect

- **While unlocked, the key is in the recorder's memory,** from login until logout, the
  30-minute idle lock, or a restart. Anyone who can read that process's memory (root, or the
  same OS user) can take it. Python cannot reliably wipe it afterwards.
- **A compromised web process.** The password passes through the web process when you log in,
  export, or change the password, so code running there can capture it. Both processes also
  run as the same OS user, so that code could reach the data files directly. The split guards
  against mistakes and misuse of the web API, not against code execution in the web process.
- **A compromised server or OS user.** Whoever controls the account the app runs as can read
  the password at the next login and change or delete the data and its history.
- **The network, without HTTPS.** Over plain HTTP the password and the session cookie cross
  the network readable. See "HTTPS" in the README.
- **Old copies after a password change.** Changing the password re-encrypts the current files.
  Earlier commits, and any copy or backup made before the change, stay encrypted under the old
  password. If the old password leaked, treat everything entered before the change as exposed.
- **Exports.** An export is your data, decrypted, in a file in your downloads folder.
- **`REGISTER_PASSWORD`.** Commands read the password from this environment variable if it is
  set. It is then visible to that shell's other programs for as long as it stays set.
- **A lost password.** There is no recovery code and no reset. The data is gone.
- **A logged-in browser can do almost everything.** While a session is live, the page can
  read every sheet and its history (earlier values included, back to the last password
  change), and can enter, change, delete and undo. Only exporting and changing the password
  ask for the password again.
- **Your browser and device.** Anyone at an unlocked browser with a live session has the
  register. Log out when you are done; **Security… → Log out everywhere** ends every session.

## Reporting a problem

Please report security problems privately, through **Report a vulnerability** on the
repository's Security tab on GitHub. If that isn't available, open an issue that says only
that you have a security report, without the details, and you will be contacted.

Include the version (`register.pyz --version` prints it), what you did, and what happened. Only the latest version gets fixes.
This is a small project maintained in spare time; reports are welcome, and there is no bounty.
