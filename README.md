# CommStat / CommStatOne / CommStat  
Situational Awareness Companion Software for JS8Call

---

## Project Context and Intended Use

CommStat and its related projects were created to support **organized HF digital communications** using **JS8Call**, with a focus on situational awareness, structured reporting, and group coordination.

Over time, these tools have been used by members and participants associated with organizations and communities such as **AmRRON**, **MPURN**, **PREPPERNET**, and other independent emergency communications and preparedness groups.

- AmRRON: https://amrron.com/  
- MPURN: https://mpurn.wordpress.com/  
- PREPPERNET: https://preppernet.com/

> **Important Notice**  
> References to AmRRON, MPURN, PREPPERNET, or any other organization are **historical and descriptive only** and do **not** imply endorsement, sponsorship, or official affiliation unless explicitly stated by those organizations.

---

## Project Lineage Overview

The CommStat ecosystem consists of multiple related projects developed independently over time:

- **CommStat** – Original application concept and implementation  
- **CommStatOne** – Python-based, cross-platform rewrite  
- **CommStat** – Community-driven rebuild and modernization effort  

Each project builds upon earlier ideas while maintaining compatibility with **JS8Call** and its API.

---

# CommStatOne Version 1.0.5  
**Released:** 10/15/2023

CommStatOne Version 1.0.5 is add-on software for JS8Call groups.  
CommStatOne is a Python version of the CommStat software **designed to run on Windows 10 and Linux**.

- **Author:** Daniel M. Hurd ~W5DMH  
- **Credit:** Special credit to **L. Hutchinson (M0IAX), England**, for the *JS8CallAPISupport* script, which enables message transmission.  
  Additional JS8Call tools: https://github.com/m0iax  
- **Repository:** https://github.com/W5DMH/commstatone

---

# CommStat Version 2.0.0  
**Released:** 6/05/2025

- **Author:** Rich W. Whittington ~KD9DSS  
- **ZIP Update Package (applies to CommStat 1.0.5 only):**  
  https://amrron.com/wp-content/uploads/2025/07/Download-2025-07-18T18_31_39.zip 

- **Highlight:** Added the AmRRON StatRep 5.1 form

---

# CommStat Version 2.3.0  
**Released:** 11/17/2025

- **Author:** Rich W. Whittington ~KD9DSS  
- **ZIP Update Package (must be applied after CommStat 2.2.0):**  
  https://amrron.com/2025/11/17/commstat-v2-3-offline-mapping/
 
- **Highlight:** Introduced Offline Maps

---

# CommStat Version 2.5.0  
**Released:** 12/25/2025

- **Author:** Manuel G. Ochoa ~N0DDK  
- **AI Code Assistant:** Claude (Anthropic), ChatGPT (OpenAI)

---

## Why CommStat?

CommStat has become the go-to situational awareness companion for JS8Call operators. What started as a focused tool for organized HF digital communications has grown into a polished, full-featured application used daily across the amateur radio and emergency preparedness communities.

Adoption continues to climb. Operators are joining the network every week, and we constantly receive positive feedback from users telling us CommStat has changed the way their nets, groups, and personal stations operate on HF digital. Whether you're a solo operator looking for better situational awareness or part of a coordinated emergency communications team, CommStat is built to make your time on the air more productive.

Visit **https://commstat.app/** to learn more.

---

## Major Features

### Dual-Path Delivery
Every StatRep, message, and alert travels simultaneously over **radio (JS8Call TCP)** and over the **internet (backbone server)**. If one path is down, the other carries the traffic. If both work, duplicates are silently discarded. Your communications get through.

### Multi-Rig Support
Run **up to three JS8Call instances at once** — different bands, different modes, different antennas — all feeding into a single CommStat window. Pick the rig you want to transmit on from a dropdown in every send dialog.

### AmRRON StatRep 5.1
Full implementation of the AmRRON 5.1 Status Report form with all twelve category scores. All-green reports compress to a single `+` character over the air for faster transmission, with automatic expansion on the receiving end.

### Group Alerts
Color-coded severity alerts (Yellow / Orange / Red / Black) with title and message fields. Transmitted via JS8Call and shared with the backbone for immediate group-wide situational awareness.

### Direct Messages
Point-to-point messaging between operators, with a contacts roster automatically built from received traffic.

### Net Check-Ins
Streamlined check-in workflow for organized nets — submit, track, and review who's on frequency.

### JS8 Email & SMS
Send email and SMS messages through the APRS gateway, directly from inside CommStat.

### Live Map with Offline Support
Real-time map plotting of station locations with grid square enrichment. **Offline maps** keep the map working when the internet doesn't.

### QRZ Integration
Optional QRZ.com lookups enrich incoming messages with grid squares, names, and locations. A built-in 30-day cache keeps things fast and minimizes API calls.

### Group Management
Unlimited user-defined groups, stored in the database. Filter all data displays by active group with a single click. Default groups (MAGNET, AMRRON, PREPPERNET) seeded on first run.

### Tools
- **Band Conditions** — live solar-terrestrial data from N0NBH
- **World Map** — HF propagation visualization
- **RSS News Feeds** — integrated news feed display with filtering and sorting

### Event-Driven UI
The interface updates the instant data arrives — no polling delays. StatRep tables, messages, alerts, and the map all refresh in real time.

### Automatic Updates
CommStat checks for new versions automatically via the backbone, so operators always know when an update is available.

### Cross-Platform
First-class support for **Windows, macOS, and Linux** — including Raspberry Pi.

**Python version note (Windows):** Python 3.13 or 3.14 is the safest choice. Python 3.15 works with the latest `install.py`, but if you see a numpy CPU error (`X86_V2`) or `install.py` stops at numpy, install Python 3.13 or 3.14 and run `python install.py` again.

---

## A Growing Community

CommStat continues to gain popularity month over month. New operators are joining the backbone regularly, and the feedback keeps coming in: faster nets, more consistent reporting, better-coordinated groups. If you're running JS8Call and you haven't tried CommStat yet — give it a spin. We think you'll see why so many operators have made it their daily driver.

---

## License & Copyright

Copyright © 2025–2026 Manuel Ochoa

This project is licensed under the **GNU General Public License v3.0**.

CommStat is derived from earlier CommStat projects originally created by **Daniel M. Hurd (W5DMH)** and later expanded by **Rich W. Whittington (KD9DSS)**.  
The original CommStat design incorporated concepts and workflows developed in collaboration with:

- **AmRRON** — https://amrron.com/  
- **MPURN** — https://mpurn.wordpress.com/  
- **PREPPERNET** — https://preppernet.com/

References to organizations are **historical and descriptive only**.

AI assistance provided by **Claude (Anthropic)** and **ChatGPT (OpenAI)**.

---

## Contact

For questions, comments, or suggestions:  
**commstat@proton.me**

Website: **https://commstat.app/**
