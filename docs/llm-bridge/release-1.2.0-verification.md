# LLM Bridge 1.2.0 verification

The frozen production revision is `ee7495319b7ea3460319d9cd6aff168fda772b1d`.
The final documentation commit changes no production or test source. PR #27
records exact-head CI, merge and release publication.

## Required gates

| Check | Result |
| --- | --- |
| Full local `python -m pytest -q` | 1,426 passed |
| `coverage run -m pytest`, `coverage combine`, `coverage report --fail-under=85` | Passed; 89% total |
| Existing Bridge/terminal coverage gate, `--fail-under=90` | Passed; 90% |
| GitHub Actions Python 3.11 and 3.15 | All jobs passed on the frozen production revision |
| Codecov project and patch checks | Both passed |
| Installed wheel outside checkout | All eight offline topics, renderer and source fallback passed |
| Missing/broken Rich | Readable source and exact tail retained |
| Installed artifact view | UTF-8/CRLF source exact; non-TTY follow rejected explicitly |
| Hermetic CLI smoke matrix | 16 checks passed; suspension disabled |
| Wheel Python source comparison | All packaged Python files match the checkout |

The package declares Rich as its only new direct runtime dependency, loaded only
for Markdown. Help and source reading work without it, tmux or mission state.

## Acceptance evidence

Real PTY/tmux tests verify visible cells and cursor visibility/coordinates at
80×24 (provider viewport 80×23), native input ownership, zoom/focus, mouse,
scrollback, resize, detach/reattach and unchanged child identity. A 64 KiB native
bracketed paste is byte exact and delivered once. A real launcher saves a 65,536
byte objective with CRLF, tabs, combining marks, emoji, literal arrow/mouse escape
sequences and a tail marker with the original SHA-256, using Save mission only.

30/32/64/256 KiB source fixtures survive composition, durable storage, amendment,
reopening and export with matching hashes. Real 64/256 KiB composer pastes pass.
The exact 1 MiB interactive ceiling and an unchanged larger file import pass.
Oversize and interrupted transactions preserve prior source; late payload cannot
become commands. Recovery files/directories use 0600/0700. Failed reads cannot
become amendment source, and failed saves retain source and a recovery path.

Full-document tests cover global Unicode search, tail navigation, source/render
changes, wrapping, horizontal code lines, heading navigation, exact export,
clipboard fallback, malformed Markdown and asynchronous renderer cleanup. An
explicit artifact follow session pauses on scrolling, reports new output, resumes
on End and labels truncation/rotation without promising pruned history.

On this machine, a 243,024-byte Markdown example first displayed source in 70.1 ms.
The initial synchronous render took 795.8 ms, so large rendering was moved off the
input path. The resulting first usable source/progress viewport took 3.9 ms,
with rendering finishing in the background in 768.5 ms. Cached paging averaged
0.04 ms over 100 operations. These are local design measurements, not CI timing
assertions or universal performance claims.

## Review and limitations

An independent agent reviewed the actual PR and reproduced three defects:
late task reads replacing help, incorrect static-view resume guidance and stale
focus markers. All were fixed and rechecked; the final independent review at the
frozen production revision found no new blocking correctness or data-loss defect.
Additional self-review and regression work fixed read-error text becoming editable
source, stale pending amendment source, failed draft creation recovery, unknown
escape fragments, overflow feedback, literal-control paste loss and wheel fallback
when an alternate-screen child has not requested mouse input. WORKLOG.md preserves
reproductions, findings, intermediate failures and continuation evidence.

Acceptance is hermetic and uses deterministic fake providers. Paid-provider
behavior across all installed CLIs and terminal emulators is unverified. An earlier
launcher test change accidentally launched real Codex with fixture prompts; those
test-owned resources were stopped, this was disclosed, and a permanent test guard
now isolates HOME, removes credentials and blocks ambient provider executables.
That incident is not acceptance evidence. No real suspend was used for validation.
Complex emoji shaping remains terminal-dependent; the documented common Unicode
cases are tested.

## Actual rendered control examples

These are real tmux captures of Bridge with deterministic injected state, not
provider execution evidence. Blank body rows are omitted below.

### Running

```text
LLM BRIDGE Release example RUNNING Bridge                                                                               
o Open full objective · Ctrl-] P Provider · ? Help                                                                      
[1 Bridge]  2 Orders  3 Stations  4 Systems  5 Log  6 Sessions  7 Palette  8 Usage                                      
> Controller: inspect Systems for observation                                                                           
Executing: none                                                                                                         
Next: none ready                                                                                                        
Orders: running 1                                                                                                       
Pending commands: 0                                                                                                     
Criteria verified: ?/? | ops 0 | events 0                                                                               
ACTIVE codex | capacity unavailable                                                                                     
>                                                                                                                       
F1-8/Tab views PgUp/Dn pages Ctrl-E add Ctrl-R edit Ctrl-] z expand Ctrl-D exit                                         
```

### Paused

```text
LLM BRIDGE Release example PAUSING Bridge                                                                               
o Open full objective · Ctrl-] P Provider · ? Help                                                                      
[1 Bridge]  2 Orders  3 Stations  4 Systems  5 Log  6 Sessions  7 Palette  8 Usage                                      
> Pausing after current provider invocation                                                                             
Executing: none                                                                                                         
Next: none ready                                                                                                        
Orders: running 1                                                                                                       
Pending commands: 0                                                                                                     
Criteria verified: ?/? | ops 0 | events 0                                                                               
ACTIVE codex | capacity unavailable                                                                                     
>                                                                                                                       
F1-8/Tab views PgUp/Dn pages Ctrl-E add Ctrl-R edit Ctrl-] z expand Ctrl-D exit                                         
```

### Completed

```text
LLM BRIDGE Release example COMPLETED Bridge                                                                             
o Open full objective · Ctrl-] P Provider · ? Help                                                                      
[1 Bridge]  2 Orders  3 Stations  4 Systems  5 Log  6 Sessions  7 Palette  8 Usage                                      
> Controller: inspect Systems for observation                                                                           
Executing: none                                                                                                         
Next: none ready                                                                                                        
Orders: completed 1                                                                                                     
Pending commands: 0                                                                                                     
Criteria verified: ?/? | ops 0 | events 0                                                                               
Last selected codex | capacity unavailable                                                                              
>                                                                                                                       
F1-8/Tab views PgUp/Dn pages Ctrl-E add Ctrl-R edit Ctrl-] z expand Ctrl-D exit                                         
```
