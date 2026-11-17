# #166 Drop the uv dependency the fastboot resolver never reaches

- Kind: pull, state: closed, by cody, created 2026-08-27T22:36:11-07:00
- Labels: none; milestone: none
- refs/pull/166/head -> main, merged: True

Homebrew stopped shipping an Intel macOS bottle for uv, which made the Intel bottle unbuildable. The dependency bought nothing: fastboot.py tries the bundled helper, then the venv python if it imports pyusb, then this interpreter if it carries the pinned pyusb, and only then uv — and the formula ships pyusb as a resource into that venv, so a Homebrew install always stops at the second rung. uv is also the transport the code deliberately ranks last, because it resolves pyusb from PyPI on every call while flashing runs on the robot own AP with no internet. No behaviour changes on any platform. Pinned by a test so an upstream bottle set cannot break a platform through a dependency nothing reaches.
