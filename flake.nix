{
  description = "Refract -- a Linux desktop shell for VITURE XR glasses";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    let
      # The udev rule is machine-wide, not per-arch: exposed as a NixOS module
      # so a system can grant USB access to the glasses the same way install.sh
      # does on Ubuntu, but the Nix way (no imperative /etc file).
      udevModule = { ... }: {
        services.udev.extraRules = ''
          # VITURE Pro XR glasses -- USB access for Refract's head-tracking SDK
          # and hardware controls. uaccess hands the seat's active user access
          # with no group membership; see udev/99-refract-xr.rules for why.
          SUBSYSTEM=="usb", ATTR{idVendor}=="35ca", MODE="0660", TAG+="uaccess"
        '';
      };
    in
    {
      # `default` is the conventional name; `refract` is kept as an alias.
      nixosModules.default = udevModule;
      nixosModules.refract = udevModule;
    }
    // flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };
        inherit (pkgs) lib;

        pythonEnv = pkgs.python3.withPackages (ps: [
          ps.pygobject3
          ps.moderngl
          ps.glfw
          ps.glcontext
          ps.numpy
          ps.pillow
        ]);

        # Everything the running shell dlopens or shells out to. Kept as one
        # list so the wrapper and the devShell cannot drift apart. `.out`
        # explicitly: gstreamer's plugins (coreelements -- fakesink, capsfilter,
        # which the filtered `! caps !` links need) live in `out`, not the
        # default `-bin` output.
        gstPlugins = with pkgs.gst_all_1; [
          gstreamer.out        # coreelements: fakesink, capsfilter, queue, ...
          gst-plugins-base.out # videoconvert, appsink
          gst-plugins-good.out
        ] ++ [ pkgs.pipewire ]; # pipewiresrc lives with pipewire

        # The typelibs live in each package's `out` output; the default output
        # of glib/gstreamer is `bin`, which does NOT carry them -- so name
        # `.out` explicitly rather than letting makeSearchPath pick the default.
        giPkgs = [
          pkgs.glib.out                       # GLib, Gio, GObject
          pkgs.gobject-introspection.out      # the DBus typelib stub
          pkgs.gst_all_1.gstreamer.out        # Gst, GstBase
          pkgs.gst_all_1.gst-plugins-base.out # GstApp
        ];

        # libz for the vendored VITURE SDK .so (its only non-libc NEEDED),
        # libGL for moderngl/glfw and the fastblit fast path, the Wayland
        # client libs glfw loads at runtime, and the GStreamer/GObject shared
        # libraries the fastblit C dlopen's by soname (libgstreamer-1.0.so.0,
        # libgstapp-1.0.so.0, libgobject-2.0.so.0).
        runtimeLibs = [
          pkgs.zlib
          pkgs.libGL
          pkgs.glfw
          pkgs.wayland
          pkgs.libxkbcommon
          pkgs.glib.out
          pkgs.gst_all_1.gstreamer.out
          pkgs.gst_all_1.gst-plugins-base.out
        ];

        typelibPath = lib.makeSearchPath "lib/girepository-1.0" giPkgs;
        gstPluginPath = lib.makeSearchPath "lib/gstreamer-1.0" gstPlugins;
        libPath = lib.makeLibraryPath runtimeLibs;
        # kscreen-doctor (display control on KDE) and zenity (the no-terminal
        # conflict/unplug dialogs) are found on PATH.
        runtimeBins = [ pkgs.kdePackages.libkscreen pkgs.zenity ];
        binPath = lib.makeBinPath runtimeBins;

        refract = pkgs.stdenv.mkDerivation {
          pname = "refract";
          version = "0.1.2";
          src = ./.;

          nativeBuildInputs = [ pkgs.makeWrapper pkgs.gcc ];
          buildInputs = [ pythonEnv pkgs.libGL ];

          # Build the optional C fast path here rather than at first run: on
          # NixOS there is no ambient cc, and fastblit.build() would just warn
          # and fall back to the slow Python path forever.
          buildPhase = ''
            runHook preBuild
            $CC -O2 -fPIC -shared -Wall \
              -o refract/core/librefract_blit.so \
              csrc/refract_blit.c -lGL
            runHook postBuild
          '';

          installPhase = ''
            runHook preInstall
            mkdir -p $out/share/refract $out/bin
            cp -r . $out/share/refract

            makeWrapper ${pythonEnv}/bin/python3 $out/bin/refract \
              --add-flags "-m refract" \
              --chdir $out/share/refract \
              --prefix GI_TYPELIB_PATH : "${typelibPath}" \
              --prefix GST_PLUGIN_SYSTEM_PATH_1_0 : "${gstPluginPath}" \
              --prefix LD_LIBRARY_PATH : "${libPath}" \
              --prefix PATH : "${binPath}"

            # refract.ctl is the Display Handoff entry point a hotkey binds to
            makeWrapper ${pythonEnv}/bin/python3 $out/bin/refract-ctl \
              --add-flags "-m refract.ctl" \
              --chdir $out/share/refract \
              --prefix GI_TYPELIB_PATH : "${typelibPath}" \
              --prefix GST_PLUGIN_SYSTEM_PATH_1_0 : "${gstPluginPath}" \
              --prefix LD_LIBRARY_PATH : "${libPath}" \
              --prefix PATH : "${binPath}"
            runHook postInstall
          '';

          meta = with lib; {
            description = "A Linux desktop shell for VITURE XR glasses";
            homepage = "https://github.com/parheliatech/refract";
            license = licenses.gpl3Plus;
            platforms = platforms.linux;
            mainProgram = "refract";
          };
        };
      in
      {
        packages.default = refract;
        packages.refract = refract;

        # `nix develop` -- run the shell straight from the tree with
        # `python -m refract`, no venv and no pip (install.sh's job on
        # Ubuntu). The same env the wrapped package bakes in.
        devShells.default = pkgs.mkShell {
          packages = [ pythonEnv pkgs.gcc pkgs.pkg-config ]
            ++ runtimeBins ++ runtimeLibs;
          shellHook = ''
            export GI_TYPELIB_PATH="${typelibPath}''${GI_TYPELIB_PATH:+:$GI_TYPELIB_PATH}"
            export GST_PLUGIN_SYSTEM_PATH_1_0="${gstPluginPath}''${GST_PLUGIN_SYSTEM_PATH_1_0:+:$GST_PLUGIN_SYSTEM_PATH_1_0}"
            export LD_LIBRARY_PATH="${libPath}''${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
            echo "refract dev shell -- build the fast path with:"
            echo "    python -m refract.core.fastblit --build"
            echo "then run:  python -m refract"
          '';
        };
      });
}
