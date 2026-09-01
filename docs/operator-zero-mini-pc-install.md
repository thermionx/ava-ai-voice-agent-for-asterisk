# Operator Zero Mini PC — Clean Installation Guide

This document describes how to install Operator Zero on a fresh Ubuntu Server mini PC.

## Installation assumptions

The mini PC starts from scratch. Do not assume Asterisk, Docker, AVA, Operator Zero, or any supporting service is already installed.

Current known-good deployment:

- Ubuntu Server 26.04 LTS
- x86_64 mini PC
- Asterisk 22.10.1
- Docker CE with Docker Compose
- Grandstream HT812
- Twilio SIP
- AVA with OpenAI Realtime
- Operator Zero household memory service
- Operator Zero web-search bridge

Example mini-PC address used in this installation:

    192.168.1.33

Mini-PC account:

    brian

Replace machine-specific addresses as appropriate.

Never commit API keys, SIP passwords, Twilio credentials, private location information, runtime databases, voicemail, or recordings to Git.

## Directory layout

The mini PC uses:

    /home/brian/AVA-AI-Voice-Agent-for-Asterisk
    /home/brian/OperatorZero
    /home/brian/operator-zero-web

The desktop may use different development paths. Do not reproduce desktop-only paths on the mini.

# 1. Install base Ubuntu packages

On the mini PC:

    sudo apt update
    sudo apt full-upgrade -y

Install basic tools:

    sudo apt install -y \
      ca-certificates \
      curl \
      wget \
      git \
      rsync \
      sqlite3 \
      ffmpeg \
      build-essential \
      pkg-config

After an OS upgrade, reboot if required.

Verify:

    hostnamectl
    uname -a
    ip addr
    ffmpeg -version | head
    sqlite3 --version

Use a stable or DHCP-reserved LAN address for the mini PC.

# 2. Install Docker

Install Docker CE using Docker's official Ubuntu repository.

    sudo install -m 0755 -d /etc/apt/keyrings

    curl -fsSL https://download.docker.com/linux/ubuntu/gpg |
      sudo tee /etc/apt/keyrings/docker.asc >/dev/null

    sudo chmod a+r /etc/apt/keyrings/docker.asc

    . /etc/os-release

    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" |
      sudo tee /etc/apt/sources.list.d/docker.list >/dev/null

    sudo apt update

    sudo apt install -y \
      docker-ce \
      docker-ce-cli \
      containerd.io \
      docker-buildx-plugin \
      docker-compose-plugin

Enable Docker:

    sudo systemctl enable --now docker

Allow the normal user to run Docker:

    sudo usermod -aG docker brian

Log out and reconnect so the new group membership takes effect.

Verify:

    docker --version
    docker compose version
    docker ps

# 3. Install Asterisk 22.10.1

Operator Zero's known-good installation uses Asterisk 22.10.1.

Do not assume that merely installing the Ubuntu Asterisk package provides an equivalent runtime. An earlier Asterisk 22.5.2 installation exhibited an AudioSocket failure with this application.

## Critical build dependencies

Two dependencies are especially important:

    libsystemd-dev
    libcurl4-openssl-dev

`libsystemd-dev` is required so the source-built Asterisk supports the Ubuntu systemd `Type=notify` service correctly.

`libcurl4-openssl-dev` is required for the Asterisk CURL dialplan functions used by Operator Zero's trusted-caller lookup.

Install build prerequisites:

    sudo apt install -y \
      build-essential \
      libedit-dev \
      libjansson-dev \
      libsqlite3-dev \
      uuid-dev \
      libxml2-dev \
      libssl-dev \
      libncurses5-dev \
      libnewt-dev \
      libsystemd-dev \
      libcurl4-openssl-dev

Download the exact known-good Asterisk release:

    cd /usr/src

    sudo wget \
      https://downloads.asterisk.org/pub/telephony/asterisk/old-releases/asterisk-22.10.1.tar.gz

    sudo tar xzf asterisk-22.10.1.tar.gz
    sudo chown -R brian:brian /usr/src/asterisk-22.10.1
    cd /usr/src/asterisk-22.10.1

Configure:

    ./configure --libdir=/usr/lib

Verify systemd support BEFORE building:

    grep HAVE_SYSTEMD include/asterisk/autoconfig.h

Required result:

    #define HAVE_SYSTEMD 1

Build:

    make -j"$(nproc)"

Before installing, verify that the important modules were built:

    find . \( \
      -name 'func_curl.so' -o \
      -name 'res_curl.so' -o \
      -name 'chan_audiosocket.so' -o \
      -name 'res_audiosocket.so' -o \
      -name 'app_audiosocket.so' \
      \) -print

Install:

    sudo make install

Restart Asterisk:

    sudo systemctl restart asterisk

Verify the version:

    sudo asterisk -rx "core show version"

Required version:

    Asterisk 22.10.1

# 4. Mandatory Asterisk capability checks

Do not continue merely because Asterisk starts.

Verify AudioSocket:

    sudo asterisk -rx "module show like audiosocket"

Required modules:

    app_audiosocket.so
    chan_audiosocket.so
    res_audiosocket.so

They should report `Running`.

Verify CURL:

    sudo asterisk -rx "module show like curl"

Required modules include:

    func_curl.so
    res_curl.so
    res_config_curl.so

Verify the dialplan function itself:

    sudo asterisk -rx "core show function CURL"

The command must display documentation for the CURL function.

If Asterisk reports:

    Function CURL not registered

or:

    Function CURLOPT not registered

STOP. Trusted-caller bypass will not work.

Install `libcurl4-openssl-dev`, reconfigure, rebuild, and reinstall Asterisk before continuing.

Also verify systemd:

    systemctl show asterisk -p ActiveState -p SubState -p Type -p NRestarts

A healthy installation should show approximately:

    ActiveState=active
    SubState=running
    Type=notify
    NRestarts=0


# 5. Copy AVA to the mini PC

From the desktop:

    rsync -av \
      --exclude '.git' \
      --exclude '__pycache__' \
      --exclude '*.pyc' \
      ~/AVA-AI-Voice-Agent-for-Asterisk/ \
      brian@192.168.1.33:/home/brian/AVA-AI-Voice-Agent-for-Asterisk/

The Git repository contains source-controlled files, but some required runtime files are intentionally local and may be ignored by Git.

Examples include:

    .env
    config/ai-agent.local.yaml
    data/operator/agents.db

Copy required local runtime files deliberately. Never commit their secrets merely to simplify deployment.

# 6. Copy Operator Zero

From the desktop:

    rsync -av \
      --exclude '.git' \
      --exclude '__pycache__' \
      --exclude '*.pyc' \
      ~/IdeaProjects/OperatorZero/ \
      brian@192.168.1.33:/home/brian/OperatorZero/

The intended mini-PC location is:

    /home/brian/OperatorZero

Do not retain desktop-only paths such as:

    /home/brian/IdeaProjects/OperatorZero

inside active mini-PC service definitions.

# 7. Install the Operator Zero memory service

The memory service runs from:

    /home/brian/OperatorZero/memory/memory_server.py

and listens on TCP port:

    8790

The systemd service should run as the normal `brian` account.

Example service:

    [Unit]
    Description=Operator Zero Household Memory Service
    After=network-online.target
    Wants=network-online.target

    [Service]
    Type=simple
    User=brian
    Group=brian
    WorkingDirectory=/home/brian/OperatorZero/memory
    ExecStart=/usr/bin/python3 /home/brian/OperatorZero/memory/memory_server.py
    Restart=on-failure
    RestartSec=3

    [Install]
    WantedBy=multi-user.target

Install the service:

    sudo cp \
      /home/brian/OperatorZero/systemd/operator-zero-memory.service \
      /etc/systemd/system/operator-zero-memory.service

    sudo systemctl daemon-reload
    sudo systemctl enable --now operator-zero-memory

Verify:

    systemctl is-active operator-zero-memory

    curl -s http://127.0.0.1:8790/health
    echo

Expected:

    {"status": "ok"}

Also verify that the intended household-memory database is being used rather than a newly created empty database.

For example:

    curl -s http://127.0.0.1:8790/caller/trusted
    echo

# 8. Install the Operator Zero web-search bridge

The web-search bridge lives at:

    /home/brian/operator-zero-web

Copy it from the desktop:

    rsync -av \
      ~/operator-zero-web/ \
      brian@192.168.1.33:/home/brian/operator-zero-web/

Its local `.env` may contain private home-location configuration.

Protect it:

    ssh 192.168.1.33 \
      'chmod 600 /home/brian/operator-zero-web/.env'

Known-good service definition:

    [Unit]
    Description=Operator Zero OpenAI Web Search Bridge
    Wants=network-online.target
    After=network-online.target

    [Service]
    Type=simple
    User=brian
    Group=brian
    WorkingDirectory=/home/brian/operator-zero-web
    EnvironmentFile=/home/brian/AVA-AI-Voice-Agent-for-Asterisk/.env
    EnvironmentFile=/home/brian/operator-zero-web/.env
    ExecStart=/usr/bin/python3 /home/brian/operator-zero-web/web_search_server.py
    Restart=on-failure
    RestartSec=3

    [Install]
    WantedBy=multi-user.target

Install and enable it:

    sudo systemctl daemon-reload
    sudo systemctl enable --now operator-zero-web

Verify:

    systemctl is-active operator-zero-web

    curl -s http://127.0.0.1:8788/health
    echo

Expected:

    {"status": "ok"}

# 9. Update machine-specific configuration

The known-good mini-PC address for this installation is:

    192.168.1.33

Important services currently use:

    Asterisk HTTP/ARI       192.168.1.33:8088
    AVA AudioSocket         192.168.1.33:8090
    Web-search bridge       192.168.1.33:8788
    Household memory        192.168.1.33:8790
    Asterisk SIP            UDP 15060

Review active AVA configuration for references to an old host.

Important files include:

    config/ai-agent.local.yaml
    config/ai-agent.yaml
    .env
    src/engine.py

Do not perform an unrestricted global replacement across the repository. Backups, examples, documentation, or historical material may legitimately contain other addresses.

After editing, syntax-check changed Python files where appropriate.

# 10. Install Asterisk configuration

Operator Zero requires appropriate Asterisk configuration for:

    /etc/asterisk/ari.conf
    /etc/asterisk/extensions.conf
    /etc/asterisk/http.conf
    /etc/asterisk/pjsip.conf
    /etc/asterisk/voicemail.conf

Back up the mini's current Asterisk configuration before replacing files:

    STAMP=$(date +%Y%m%d_%H%M%S)
    sudo mkdir -p "/etc/asterisk/operator-zero-backup-$STAMP"
    sudo cp -a /etc/asterisk/. \
      "/etc/asterisk/operator-zero-backup-$STAMP/"

Do not blindly copy `asterisk.conf` or `modules.conf` from another installation.

Those files can contain module-directory and installation-specific settings that differ between a distribution build and a source build.

The known-good source installation uses:

    /usr/lib/asterisk/modules

After installing configuration:

    sudo systemctl restart asterisk

Verify:

    sudo asterisk -rx "core show version"
    sudo asterisk -rx "http show status"
    sudo asterisk -rx "pjsip show endpoints"
    sudo asterisk -rx "ari show apps"

The ARI application should include:

    asterisk-ai-voice-agent

# 11. Asterisk HTTP and ARI

The current installation has Asterisk HTTP/ARI available at:

    192.168.1.33:8088

AVA must be able to connect to this endpoint.

Do not put ARI credentials into Git.

After AVA starts, its logs should show a successful ARI HTTP connection followed by a successful ARI WebSocket connection.

# 12. Build and start AVA

On the mini PC:

    cd /home/brian/AVA-AI-Voice-Agent-for-Asterisk

    docker compose build
    docker compose up -d

Verify:

    docker compose ps

Expected services include:

    ai_engine
    admin_ui
    local_ai_server

The OpenAI Realtime provider must initialize successfully for Operator Zero.

Some optional local/hybrid AI providers may report missing models or credentials. Those warnings are not necessarily fatal when Operator Zero is configured to use OpenAI Realtime.

# 13. Verify AVA dependencies from inside the container

First verify ffmpeg:

    cd /home/brian/AVA-AI-Voice-Agent-for-Asterisk
    docker compose exec ai_engine ffmpeg -version

Then verify that the AVA container can reach the memory and web services:

    docker compose exec ai_engine python - <<'PY2'
    import urllib.request

    for url in (
        "http://192.168.1.33:8790/health",
        "http://192.168.1.33:8788/health",
    ):
        print(url)
        print(urllib.request.urlopen(url, timeout=3).read().decode())
    PY2

Both services should return healthy responses.

# 14. Grandstream HT812

The analog household telephone connects through a Grandstream HT812.

Known-good FXS Port 1 settings include:

    SIP User ID:        100
    SIP Authenticate ID: 100
    Profile ID:         PROFILE 1

The SIP password is private and must not be documented in Git.

PROFILE 1 must point its Primary SIP Server at the mini PC.

Current installation:

    192.168.1.33:15060

Do not factory-reset the HT812 during a server migration.

Change only settings that actually need to change.

Verify registration from Asterisk:

    sudo asterisk -rx "pjsip show endpoints"

Endpoint `100` should have a contact for the HT812.

# 15. Twilio and inbound routing

Asterisk should also have the Twilio PJSIP configuration loaded.

Verify:

    sudo asterisk -rx "pjsip show endpoints"

Expected endpoints include:

    100
    twilio

Twilio credentials must remain outside Git.

The exact incoming DID route performs the Operator Zero trusted-caller lookup before deciding whether to ring the house directly or invoke Operator Zero.

# 16. Router/NAT configuration

When moving Operator Zero from another computer, remember that router forwarding is independent of the copied software configuration.

If SIP traffic was previously forwarded to the desktop, change the internal forwarding destination to the mini PC.

Current mini:

    192.168.1.33

Known-good Asterisk SIP listener:

    UDP 15060

Preserve the already-working external protocol and port mapping unless there is a specific reason to change it.

A useful diagnostic rule:

If an outside call fails immediately and Asterisk shows absolutely no incoming call, investigate upstream SIP/router/NAT delivery before changing AVA.


# 17. Trusted-caller bypass

Operator Zero remembers callers who have successfully been accepted by the household.

A caller must NOT become trusted merely because:

- Operator Zero screened the call.
- The household phone rang.
- The destination answered but the private announcement did not complete.
- The transfer failed.

A caller becomes trusted only after the successful acceptance sequence:

1. Operator Zero screens the outside caller.
2. The intended household recipient answers.
3. The private announcement completes.
4. The outside caller is successfully bridged to the household recipient.

The trust decision is enforced by application/call-control logic and must not depend only on the AI prompt.

Check trusted callers directly:

    curl -s http://127.0.0.1:8790/caller/trusted
    echo

For an individual number:

    curl -s \
      "http://127.0.0.1:8790/caller?phone=19252165848"
    echo

A trusted caller should contain:

    "trusted_caller": "true"

The incoming Asterisk dialplan normalizes the caller number and queries the memory service with `CURL()`.

If CURL support is missing, the memory service can be completely healthy while trusted-caller bypass still fails.

# 18. Test trusted-caller bypass

Use an outside number that is already marked trusted.

Open the Asterisk console on the mini:

    sudo asterisk -rvvv

Call the household number.

The console should show:

- the incoming Twilio call,
- caller-number normalization,
- a non-empty trusted-caller lookup result,
- `IS_TRUSTED=1`,
- trusted-caller bypass,
- and `Dial(PJSIP/100,30)`.

The trusted caller should NOT hear Operator Zero's screening greeting.

If the caller is sent to Operator Zero, immediately check:

    sudo asterisk -rx "module show like curl"
    sudo asterisk -rx "core show function CURL"

Also query the memory service directly to distinguish a memory problem from an Asterisk CURL problem.

# 19. Voicemail

Operator Zero uses mailbox:

    100@default

Verify:

    sudo asterisk -rx "voicemail show users"

Unanswered incoming calls should route to voicemail.

The household can also reach VoiceMailMain internally through the configured internal dialplan and Operator Zero voicemail tool.

Test both:

1. Allow an outside call to go unanswered and leave a message.
2. Retrieve the message from the household phone.

Do not copy or commit private voicemail recordings to Git.

# 20. Internal Operator Zero test

From the household analog phone, dial:

    0

Expected path:

    HT812
      -> Asterisk
      -> Stasis
      -> AudioSocket
      -> AVA
      -> OpenAI Realtime
      -> Operator Zero

A successful test should provide normal two-way conversation with Operator Zero.

If the AudioSocket connects and immediately disappears, verify the exact Asterisk version and AudioSocket modules before investigating OpenAI.

# 21. Unknown outside-caller test

Call the household number from a number that is not trusted.

Expected behavior:

1. Twilio delivers the call to Asterisk.
2. Asterisk determines that the caller is not trusted.
3. The call enters the `operator_zero_incoming` path.
4. Operator Zero obtains the caller's identity and intended recipient.
5. Once both are known, Operator Zero says:

       One moment, please.

6. The household phone rings.
7. The recipient receives the private caller announcement.
8. If accepted, the outside caller is bridged.
9. Only after successful acceptance does the caller become trusted.

A caller does not need to provide a reason for the call or a business/company name when identity and recipient are already known.

# 22. No-answer test

Use an untrusted outside caller.

Complete screening, but do not answer the household phone.

Expected behavior:

- the household phone rings,
- the caller is not marked trusted,
- the outside caller is routed to voicemail.

Afterward check:

    curl -s http://127.0.0.1:8790/caller/trusted
    echo

Verify that the no-answer call did not incorrectly create a trusted caller.

# 23. AVA and ARI verification

Check AVA:

    cd /home/brian/AVA-AI-Voice-Agent-for-Asterisk
    docker compose ps

Check recent AI-engine logs:

    docker compose logs --tail=100 ai_engine

Check Asterisk ARI:

    sudo asterisk -rx "ari show apps"

Expected ARI application:

    asterisk-ai-voice-agent

AVA logs should show successful ARI HTTP and WebSocket connectivity.

# 24. Service startup

The following components must survive a reboot:

    asterisk
    docker
    operator-zero-memory
    operator-zero-web
    AVA Docker containers

Verify systemd enablement:

    systemctl is-enabled asterisk
    systemctl is-enabled docker
    systemctl is-enabled operator-zero-memory
    systemctl is-enabled operator-zero-web

Inspect Docker restart policies:

    docker inspect \
      -f '{{.Name}} restart={{.HostConfig.RestartPolicy.Name}}' \
      $(docker ps -aq)

Do not assume `docker compose up -d` alone guarantees that every container will automatically return after a reboot. Verify the configured restart policies.

# 25. Full reboot test

A production installation is not complete until it survives a reboot.

On the mini:

    sudo reboot

After reconnecting, check:

    systemctl is-active asterisk
    systemctl is-active docker
    systemctl is-active operator-zero-memory
    systemctl is-active operator-zero-web

Then:

    cd /home/brian/AVA-AI-Voice-Agent-for-Asterisk
    docker compose ps

Verify Asterisk again:

    sudo asterisk -rx "core show version"
    sudo asterisk -rx "module show like audiosocket"
    sudo asterisk -rx "module show like curl"
    sudo asterisk -rx "pjsip show endpoints"
    sudo asterisk -rx "ari show apps"

Finally repeat an internal dial-0 call and an outside telephone call.

# 26. Troubleshooting: AudioSocket closes immediately

Observed symptom on an earlier installation:

    Failed to read header from AudioSocket because:
    Resource temporarily unavailable

followed by:

    Failed to receive frame from AudioSocket server

AVA could accept the AudioSocket TCP connection, but Asterisk immediately closed it before useful media was exchanged.

That machine was running Ubuntu's Asterisk 22.5.2 package.

Moving the mini to the known-good Asterisk 22.10.1 source build resolved the immediate AudioSocket teardown.

Check:

    sudo asterisk -rx "core show version"

and:

    sudo asterisk -rx "module show like audiosocket"

# 27. Troubleshooting: Asterisk repeatedly restarts after source build

A source-built Asterisk may appear to run normally but systemd can remain in:

    ActiveState=activating
    SubState=start

and terminate Asterisk after the service startup timeout.

Check:

    systemctl show asterisk \
      -p ActiveState \
      -p SubState \
      -p Type \
      -p NRestarts

Also check the source configuration:

    grep HAVE_SYSTEMD \
      /usr/src/asterisk-22.10.1/include/asterisk/autoconfig.h

Required:

    #define HAVE_SYSTEMD 1

If systemd support is absent:

    sudo apt install -y libsystemd-dev

Then rebuild:

    cd /usr/src/asterisk-22.10.1
    make distclean
    ./configure --libdir=/usr/lib

Verify `HAVE_SYSTEMD` before continuing.

Then:

    make -j"$(nproc)"
    sudo make install
    sudo systemctl restart asterisk

# 28. Troubleshooting: every caller appears untrusted

Symptoms:

- memory service says the caller is trusted,
- but Asterisk still sends the caller to Operator Zero.

Watch a call using:

    sudo asterisk -rvvv

If the console reports:

    Function CURLOPT not registered

or:

    Function CURL not registered

the problem is the Asterisk build, not caller-number normalization or the memory database.

Install:

    sudo apt install -y libcurl4-openssl-dev

Then completely reconfigure and rebuild Asterisk.

Afterward verify:

    sudo asterisk -rx "module show like curl"
    sudo asterisk -rx "core show function CURL"

# 29. Troubleshooting: outside call never reaches Asterisk

Run:

    sudo asterisk -rvvv

Then make the outside call.

If absolutely nothing appears in the Asterisk console, the failure is upstream of the Asterisk dialplan.

Check:

- router/NAT forwarding,
- destination LAN address,
- Twilio SIP configuration,
- firewall,
- and SIP port configuration.

During the original mini-PC migration, the router was still forwarding traffic to the old desktop computer.

Changing the forwarding destination to the mini PC restored inbound calls.

# 30. Troubleshooting: old machine address remains in configuration

Search active configuration:

    grep -R "192\.168\.1\." \
      /home/brian/AVA-AI-Voice-Agent-for-Asterisk/config \
      /home/brian/AVA-AI-Voice-Agent-for-Asterisk/.env \
      /home/brian/OperatorZero \
      /home/brian/operator-zero-web \
      2>/dev/null

Review every result.

Do NOT automatically replace every address returned by this search.

The objective is to eliminate stale addresses from active runtime configuration without modifying backups, examples, documentation, or unrelated data.

# 31. Known-good ports

The current installation uses:

    15060/UDP   Asterisk SIP
    8088/TCP    Asterisk HTTP/ARI
    8090/TCP    AVA AudioSocket
    8788/TCP    Operator Zero web-search bridge
    8790/TCP    Operator Zero household memory

Verify listeners when troubleshooting:

    sudo ss -lntup

# 32. Final installation checklist

Do not consider a clean installation complete until all of these have been verified.

Host:

    [ ] Mini PC has stable LAN address
    [ ] Docker installed
    [ ] Docker enabled at boot
    [ ] Asterisk enabled at boot

Asterisk:

    [ ] Asterisk 22.10.1
    [ ] HAVE_SYSTEMD 1
    [ ] app_audiosocket.so Running
    [ ] chan_audiosocket.so Running
    [ ] res_audiosocket.so Running
    [ ] func_curl.so Running
    [ ] res_curl.so Running
    [ ] CURL() registered
    [ ] ARI application registered

Operator Zero services:

    [ ] Memory service active
    [ ] Memory /health returns OK
    [ ] Correct household database present
    [ ] Web-search service active
    [ ] Web-search /health returns OK

AVA:

    [ ] ai_engine Up
    [ ] admin_ui Up
    [ ] local_ai_server Up/healthy
    [ ] ai_engine reaches memory service
    [ ] ai_engine reaches web-search service
    [ ] ARI WebSocket connected
    [ ] OpenAI Realtime provider ready

Telephony:

    [ ] HT812 endpoint 100 registered
    [ ] Twilio endpoint configured
    [ ] Router forwards to mini PC
    [ ] Internal dial 0 reaches Operator Zero
    [ ] Unknown outside caller reaches Operator Zero
    [ ] Household phone rings after screening
    [ ] Private announcement works
    [ ] Accepted caller is marked trusted
    [ ] Trusted caller subsequently bypasses Operator Zero
    [ ] No-answer caller is NOT trusted
    [ ] No-answer call reaches voicemail
    [ ] Household voicemail retrieval works

Recovery:

    [ ] Reboot mini PC
    [ ] All systemd services return
    [ ] AVA containers return
    [ ] HT812 re-registers
    [ ] Internal call works after reboot
    [ ] Outside call works after reboot

# 33. Git and security

Source control should contain:

- source code,
- safe configuration templates,
- documentation,
- deployment scripts that contain no secrets.

Do not commit:

- `.env` files containing secrets,
- OpenAI API keys,
- Twilio credentials,
- SIP passwords,
- ARI passwords,
- private location coordinates,
- `agents.db`,
- household caller databases,
- voicemail recordings,
- generated call audio,
- runtime logs.

A successful installation must not depend on secrets being stored in the Git repository.

# 34. Future deployment automation

This manual procedure should eventually become a bootstrap/deployment script.

The script should not simply run commands and print "success."

It should verify each required capability and stop with a clear error when a mandatory component is missing.

In particular, an automated installer should verify:

    Asterisk version == 22.10.1
    HAVE_SYSTEMD == 1
    AudioSocket modules loaded
    CURL modules loaded
    CURL() registered
    ARI available
    memory service healthy
    web-search service healthy
    AVA containers healthy
    HT812 registered

This is important because two installations reporting the same Asterisk version can still have different compiled capabilities.

The missing systemd and CURL dependencies encountered during the original mini-PC deployment are examples of failures that a future installer should detect automatically.
