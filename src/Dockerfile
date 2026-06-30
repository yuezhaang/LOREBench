FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update -y
RUN apt-get install -y curl git locales patch bzip2 gawk g++ gcc autoconf automake bison libc6-dev libffi-dev libgdbm-dev libncurses5-dev libsqlite3-dev libtool libyaml-dev make patch pkg-config sqlite3 zlib1g-dev libgmp-dev libreadline-dev libssl-dev libfontconfig1-dev libwayland-dev libgio2.0-cil-dev libgtk-3-dev libwebkit2gtk-4.1-dev libxdo-dev gettext python3 python3-dev python3-pip
RUN locale-gen en_US.UTF-8
ENV LANG=en_US.UTF-8

# Perl
RUN apt-get install -y cpanminus perl
RUN cpanm Dist::Zilla

ARG USERNAME
ARG GID
ARG UID
RUN groupadd -g ${GID} ${USERNAME} \
    && useradd -m -u ${UID} -g ${GID} -s /bin/bash ${USERNAME}
USER ${USERNAME}

# Rust
ENV CARGO_HOME="/home/${USERNAME}/cargo"
RUN bash -o pipefail -c \
"curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain nightly"
ENV PATH="/home/${USERNAME}/cargo/bin:${PATH}"
ENV CARGO_HOME="/home/${USERNAME}/.cargo"

# Julia
RUN bash -o pipefail -c \
"curl -fsSL https://install.julialang.org | sh -s -- -y"
ENV PATH="/home/${USERNAME}/.juliaup/bin:${PATH}"
RUN pip install sympy # For Symbolics.jl

# Haskell
RUN bash -o pipefail -c \
"curl --proto '=https' --tlsv1.2 -sSf https://get-ghcup.haskell.org | BOOTSTRAP_HASKELL_NONINTERACTIVE=1 BOOTSTRAP_HASKELL_GHC_VERSION=9.10.3 sh"
ENV PATH="/home/${USERNAME}/.ghcup/bin:$PATH"
ENV PATH="/home/${USERNAME}/.cabal/bin:$PATH"

# Elixir
RUN bash -o pipefail -c \
"curl -fsSL https://elixir-lang.org/install.sh | sh -s -- elixir@1.19.3 otp@28.1"
ENV PATH="/home/${USERNAME}/.elixir-install/installs/otp/28.1/bin:$PATH"
ENV PATH="/home/${USERNAME}/.elixir-install/installs/elixir/1.19.3-otp-28/bin:$PATH"

WORKDIR /repos