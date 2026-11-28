# Rt native Sony Settings adapter

This directory contains the first native Settings SUPRX for Rt:Settings Extended.

## Runtime target

- Process/title: Sony Settings, `NPXS10015`
- taiHEN section: `*NPXS10015`
- Module: `ur0:tai/rtsettings.suprx`
- Firmware target for the first device pass: PS Vita 1000 OLED, firmware 3.65 Ensō.

The module is user-mode. It does not replace HENkaku, does not install a kernel plugin, and does not rewrite Sony's root XML resource on disk.

## Current native page

The module waits for `system_settings_core.suprx` using a paired `sceKernelLoadStartModule` hook, then installs:

- A paired `scePafMiscLoadXmlLayout` hook in `SceSettings`. The later member of the pair performs the bounded insertion, so intervening plugin hooks (including HENkaku's root XML replacement) run before Rt edits the final XML.
- `scePafToplevelInitPluginFunctions` in `SceSettings`: wraps the root Settings button callback and handles the Rt Manager Status and Plugin Configuration Audit actions.
- `scePafToplevelGetText` in `SceSystemSettingsCore`: resolves the Rt status dialog and hint strings, including a bounded dynamic status message.
- `rt_settings_runtime.c`: portable read-only config-state evaluator, shared with host regression and sanitizer tests.\n- Shared plugin inventory and compatibility evaluators: a bounded, read-only audit summary with fail-closed error reporting.

The paired-hook ordering follows the established taiHEN Settings-plugin pattern: each pair is installed back-to-back and the active member is selected by inspecting the hook chain. The second load hook also ensures Rt observes the Settings-core load after intervening load hooks.

The visible path is:

```text
Settings
└── Rt:Settings Extended
    └── Preferences & Settings Management
        └── Rt Manager
            ├── Rt Manager Status\n            └── Plugin Configuration Audit
```

The status action now performs a read-only live inspection of `ur0:tai/config.txt` and `ur0:tai/rtsettings.suprx`. It distinguishes an active entry, a disabled entry, a missing section/entry, duplicate sections/entries, an unreadable/oversized config and a missing module file. It reports `READY` only when exactly one active entry and the module file are both present. It never repairs or writes the config. The Plugin Configuration Audit performs a separate bounded read-only scan and reports known/unknown active entries, disabled entries, known blocking conflicts, review findings and malformed lines. Compatibility is evaluated only for recognized active plugins; unknown entries remain visible in the counts and are never treated as safe. It does not activate, disable, install or remove plugins. Full per-plugin rows and configuration actions remain in Cockpit, and Tai Saver / operational feature backends are not yet connected to this native page.

## Safety behavior

- XML matching is bounded and checks the exact root plugin marker and root ID.
- The original Sony XML is passed through unchanged unless a complete insertion succeeds.
- Repeated XML loads are idempotent; an existing Rt ID is never duplicated.
- The hook buffer is module-owned and remains valid while the PAF parser consumes it.
- Hook installation is all-or-nothing for the page: the XML hook is installed only after the text and callback hooks succeed.
- All page hooks are released when the Settings core unloads and on module stop.
- No hook failure changes Sony Settings files or taiHEN configuration.

## Build

Run inside the official `vitasdk/vitasdk` container:

```sh
make -C native-settings clean all
```

The output is `native-settings/rtsettings.suprx`.

## Important validation boundary

The PAF import NIDs and lifecycle pattern are based on inspected HENkaku, YAMT and Wi-Fi Turbo implementations. The source compiles in CI only after the VitaSDK job passes. No physical PS Vita test is claimed by this repository change. The exact menu rendering, Settings reload behavior, and coexistence with the user's active plugin chain still require testing on the target 3.65 Ensō console.
