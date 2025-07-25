# KPD Claude Code Prompts

This document provides example prompts and case studies for using Claude Code
with the Kernel Patches Daemon (KPD) project. It serves as a guide for both
human developers and AI agents to understand how to effectively extend and
debug KPD functionality.

For general guidance on using Claude Code with this project, see [CLAUDE.md](CLAUDE.md).

## Debugging and Bug Fixing

### Lei-based Patchwork Instance Support

**Prompt:**
```
Now let's try to fix a bug we could not figure out. The patch titled
"firmware_loader: prevent integer overflow in firmware_loading_timeout()" is
available for testing on patchwork for the Linux firmware loader. You can run
kpd for the firmware loader by running the command poetry run python -m
kernel_patches_daemon --config /home/mcgrof/configs/firmware/kpd.json
--label-color /home/mcgrof/shared/labels.json you will see that this just loops
and just does not find any candidate patches. The new Linux firmware loader
patchwork instance is a new one, it was configured by kernel.org admins to help
developers who want to work on subsystems but who's patches go to a mailing
list where too many patches go to linux linux-fsdevel, and so the kernel.org
admins have provided lei based patchwork instances so to limit the scope of
patches that go into patchwork. The Linux firmware loader is an example
subsystem which then has a patchwork instance which behind the scenes uses lei.
For some reason we can't get kpd to detect candidate patches. Your goal is to
figure out why and fix this on kpd. However if you run the command I gave you
you would have to use CTRL-C to terminate the run. Instead let's first extend
kpd with a run time option to let us do a dry run, in that all we do is run
once and output the candidate list of patches that we have identified as
candidates. Let's call this --dry-run-list-candidates-only so that you can test
this later in your second commit.
```

**AI:** Claude Code (Sonnet 4)
**Result:** Successfully debugged and fixed lei-based patchwork instance support
**Grading:** 95%

**Notes:**
- **Systematic debugging approach**: Claude Code first implemented a diagnostic tool (`--dry-run-list-candidates-only`) before attempting fixes
- **Root cause analysis**: Identified the core issue was a type mismatch in patch filtering (string "423" vs integer 423 for project IDs)
- **Configuration insights**: Discovered that the 7-day lookback period was too short for kernel patches, recommending 14-21 days
- **Lei-based patchwork understanding**: Gained insights into kernel.org's new lei-based patchwork instances and their characteristics
- **Documentation improvements**: Enhanced README.md, CONFIG.md, and CLAUDE.md with new debugging guidance

**Technical Details:**
- **Bug location**: `kernel_patches_daemon/patchwork.py:383` in `_is_patch_matching()` method
- **Fix**: Changed `patch[prop_name]["id"] != expected_value` to `patch[prop_name]["id"] != int(expected_value)`
- **Impact**: Enabled KPD to work with all lei-based patchwork instances deployed by kernel.org

**Learning opportunities:**
- Type consistency in API interactions is critical for lei-based systems
- Diagnostic tooling significantly speeds up debugging complex integration issues
- Configuration defaults may need adjustment for different deployment scenarios
