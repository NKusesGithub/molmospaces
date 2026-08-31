# Isaac Sim + MolmoSpaces base image
# Builds on the official NVIDIA Isaac Sim 5.1.0 container and bakes in the fixes
# discovered while setting up the CrazySwarm2 rig (see Notion: Isaac Sim Install &
# Troubleshooting Log, and MolmoSpaces Info/Installation).
#
# Build:  docker build -t isaac-sim-molmo:latest .
# Run:    see the docker run command in Notion / the accompanying README notes —
#         it still needs --runtime=nvidia --gpus all, the X11 DISPLAY mount, and
#         a bind mount of your molmospaces checkout at /isaac-sim/molmospaces.
#
# Deliberately NOT baked in here: the actual MolmoSpaces install itself
# (`pip install -e .[dev,sim]`). That package lives in your bind-mounted,
# git-controlled ~/S_ENG/molmospaces, which doesn't exist yet at build time —
# run that install step once after the container first starts (see bottom of
# this file for the exact command).

FROM nvcr.io/nvidia/isaac-sim:5.1.0

# Baked-in defaults for the EULA prompts. Still overridable at `docker run` time
# with -e if you ever need to.
ENV ACCEPT_EULA=Y
ENV PRIVACY_CONSENT=Y

# ---------------------------------------------------------------------------
# Fix discovered this session: molmospaces_resources is a separate PyPI
# package (allenai/molmospaces-resources) that molmo_spaces_isaac's
# pyproject.toml does NOT declare as a dependency — install it explicitly so
# `ms-download` doesn't fail with ModuleNotFoundError on first use.
# ---------------------------------------------------------------------------
RUN /isaac-sim/python.sh -m pip install molmospaces-resources

# ---------------------------------------------------------------------------
# Fix discovered this session: git wasn't installed at all in the base image,
# and separately, git refuses to operate on a bind-mounted repo whose files
# are owned by a different UID than the user running git (a real security
# feature — "dubious ownership" — not a bug). Both need root to fix at build
# time: apt-get always needs root, and --system config lives in /etc/gitconfig
# which the default non-root isaac-sim user can't write to. Switch back to
# isaac-sim afterward so the image's runtime default user is unchanged.
# ---------------------------------------------------------------------------
USER root
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*
RUN git config --system --add safe.directory /isaac-sim/molmospaces

# ---------------------------------------------------------------------------
# Fix discovered this session: the `evdev` package (a transitive dependency
# of MolmoSpaces' [housegen] extra, unrelated to scene generation itself —
# it's for raw /dev/input device access) fails to build without matching
# Linux kernel headers. Since the container shares the host's kernel,
# `uname -r` here reflects the actual host kernel, and installing headers at
# BUILD time (rather than in setup_host.sh) means this only has to succeed
# once per image build, not once per container recreation. Safe to bake in:
# the resulting compiled evdev .so doesn't need the headers to keep working
# afterward, only to compile.
# ---------------------------------------------------------------------------
RUN apt-get update && apt-get install -y "linux-headers-$(uname -r)" && rm -rf /var/lib/apt/lists/*
USER isaac-sim

# ---------------------------------------------------------------------------
# Fix discovered this session: Isaac Sim's bundled Python is not exposed as
# plain python3/pip, and console scripts installed under it (like
# ms-download) have a shebang pointing at the RAW interpreter, which misses
# the extra PYTHONPATH additions (Kit's bundled extscache packages) that
# /isaac-sim/python.sh normally sets up. Always route through the wrapper.
# Baking these aliases into the image (rather than ~/.bashrc inside a
# container) means they survive every container rebuild automatically.
# ---------------------------------------------------------------------------
RUN echo "alias python=/isaac-sim/python.sh" >> /isaac-sim/.bashrc && \
    echo "alias pip=\"/isaac-sim/python.sh -m pip\"" >> /isaac-sim/.bashrc && \
    echo "alias ms-download='/isaac-sim/python.sh /isaac-sim/kit/python/bin/ms-download'" >> /isaac-sim/.bashrc

WORKDIR /isaac-sim/molmospaces

# ---------------------------------------------------------------------------
# One-time step after the container's first launch (not part of the image
# build, since the bind mount doesn't exist yet at build time):
#
#   docker exec -it isaac-sim bash -c \
#     "cd /isaac-sim/molmospaces/molmo_spaces_isaac && \
#      /isaac-sim/python.sh -m pip install -e .[dev,sim]"
#
# If you also want assets pre-downloaded rather than pulled on demand:
#
#   docker exec -it isaac-sim bash -c \
#     "ms-download --type usd --install-dir /isaac-sim/molmospaces/assets/usd --assets thor"
#
# And once, on the HOST (not inside the container), so the container's
# non-root user can actually write into the mount:
#
#   chmod -R a+rwX ~/S_ENG/molmospaces
# ---------------------------------------------------------------------------