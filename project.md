# SentrySDR Project Brief

### Requirements:
- PWA, not native
- GUI for setting frequencies / alpha tags
- GUI for listening / managing recordings
- Support for multiple SDRs
- Background, headless recordings
- Schedule recordings?
- Call-based, not constant recording
- Automatic gain control?
- AIS/ADSB integration?
- Feed to RDIO / ClearCut?

### Server:
- Connect to SDR(s)
- Tune to specified channel(s)
- Listen, log, and record calls
- Send to web interface / clearcut / RDIO
- Set as master / slave, allow slave instances to be configured by master UI and send all their data to master for processing / display

### Web: (Admin side)
- Define SDR source
    - SDR ID / Serial Number
    - Antenna type / frequency range
    - SDR Name / Description
- Define frequency targets
    - Frequency
    - Name / Alpha Tag
    - Modulation
    - Mode?
    - ClearCut Path / ID for upload
    - RDIO Path for upload
- Define recorder
    - Match target to source
    - Allow setting schedule time / dates
        - I.e. Record 123.45 from 1600-1800 every Tuesday on source 1
- Manage user accounts
    - Modify Name
    - Modify Email
    - Reset password
    - Modify user permissions
    - See user audit logs
    - See user IP / source
    - Ban a user by email or by IP
    - Approve / deny requested users

### Web: (User side)
- See current active recorders
- See recorder schedule (if applicable)
- Listen live
- Listen to recordings
- Download recordings
- See ADSB / AIS maps
- Link to active clearcut page and / or pull clearcut transcriptions and show under recordings
- Manage their own user account
- Sign up for a user account (Does NOT inherently give user access, just allows them to "request an account")

### Use Cases for Clarification:
- Main server is deployed in a fixed location. During RDF operations, a user can set up an SDR with a tuned 380-460MHz antenna as Source 1. The user can then specify 444.444 as the current target, named "K1ABC". The user can then monitor this receiver while in the field, and have recorded proof of interference for use in an FCC report.
- Main server is deployed in a fixed location. During an airshow, a user can take a portable feeder with a raspberry pi and two SDRs into the field. This can be set as the "Slave", and told to feed to the main server. One receiver can be set up with an antenna tuned for 1090MHz for ADSB, the other can be set with a VHF Airband antenna. The ADSB receiver can be set to the "ADSB" recorder, which pipes it to TAR1090. The VHF receiver can be set to the "Airboss" recorder, which records and displays the calls to the user as they are received. The user can also look at the ADSB map on the web app. Rather than needing to change the deployment every time, the user can just use the main servers deployed FQDN for this.
- Main server is deployed in a fixed location. A user can specify a receiver, then attach the target frequency of 444.444MHz. The user can then set that recorder to only record from 10pm to 11pm on Tuesdays, to capture a ham radio Net. If the source is unassigned, it proceeds as normal. If the source is already assigned a target, the schedule will override the currently assigned target for that specified amount of time. Once the time is completed, the recorder will switch back to the originally assigned target.

### Other notes
- This should be highly customizable. Users should be able to see all the current sources regardless of assigned status, and assign a target to a source if it is unlocked or unused.
- Admins should be able to lock a recorder so a user may not change it's assignment without a password.
- Admins should be able to override a users recorder assignment
- The application should keep audit logs for a customizable length of time
- The application should keep the recording files for a customizable length of time
- The sysadmin should be able to specify the location for the logs and the recording files.
- A recording should be able to be protected so it is not automatically deleted.
- Recordings should be categorized separately from the source/target/recorder interface - if a source is assigned a target and is recording, a user should be able to listen to current / previous recordings from that window. However, a user should also be able to view / review recordings from past pairings, even if they are not currently set up in the UI.
- When starting the application, the sysadmin should be able to specify SDR serial numbers / IDs that the application will have available to it. The application should not lock any sources that are not specified in the configuration.
- The application's web UI should be friendly to reverse proxy, so it may be viewed from localhost or proxied through a domain name.
- The application should be friendly to VPN / Tailscale connections. If the slave / remote server is deployed, it should be able to connect directly to the master / primary server over the public internet (whether via IP or by domain name), or via it's Tailscale / VPN address.
- Passwords should be hashed and stored in a secure way
- Application should leverage EXISTING packages rather than redefining new ones - like the RTL_SDR suite of tools, the existing P25 decoders, etc.