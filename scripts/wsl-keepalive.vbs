' Keep the WSL2 distro alive so the Agent Docker Platform stack keeps running.
'
' Why this is needed:
'   WSL terminates a distro once the last wsl.exe session exits. systemd-logind
'   then logs "The system will power off now!" and the whole VM shuts down,
'   taking every Docker container with it. Background systemd services do NOT
'   keep the distro alive - only an attached session does.
'
' What it does:
'   Starts one hidden, long-lived `sleep infinity` session. That single session
'   pins the distro so dockerd and all containers stay up.
'
' Usage:
'   Double-click this file, or drop a shortcut to it in the Startup folder:
'     Win+R -> shell:startup
'
' To stop it:
'   wsl --shutdown          (stops the distro and this keepalive together)
'   or: taskkill /IM wsl.exe /F

Const DISTRO = "Ubuntu-24.04"

Set shell = CreateObject("WScript.Shell")
' 0 = hidden window, False = do not wait for it to finish
shell.Run "wsl.exe -d " & DISTRO & " -- /usr/bin/sleep infinity", 0, False
