# Security

Bake Master is open source and has no account, activation or licensing service.

Please report installer path traversal, unintended source-scene modification or native crashes through a minimal reproducible Issue. Avoid attaching confidential production assets, credentials or personal file paths. If a report contains sensitive material, first open an Issue asking for a private contact channel without publishing the material.

The installer validates the package's SHA-256 file list and uses a staged directory replacement. The public updater checks the official release archive and its checksum, validates archive paths, and stages an update before replacing installed files. These checks detect corruption and unsafe paths; obtain packages from the official GitHub Releases page. A checksum distributed through the same GitHub repository is not an independent publisher signature. Release integrity depends on HTTPS and control of that repository and its publishing account.

Automatic updates are enabled by default and can be disabled in the UI. Checking and downloading an update contacts public GitHub services without a user token; failures do not invalidate the installed plugin. The updater does not execute a downloaded installer or unload a native module in use. Changes to loaded native code are deferred until a new Maya process. Local administrators or processes with write access can modify the installed open-source code; this is not a runtime anti-tamper system.

Only the latest open-source release is maintained. Historical commercial releases are retired.
