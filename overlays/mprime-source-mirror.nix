{
  meta = {
    reason = "download.mersenne.ca redirects the prime95 source zip to a Google Drive virus-scan page, so nixpkgs' mprime src fetches HTML instead of the archive; mersenne.org serves the same file directly";
    added = "2026-09-02";
    upstream = "https://github.com/NixOS/nixpkgs/blob/master/pkgs/by-name/mp/mprime/package.nix";
  };
  dropWhen =
    pkgs: !builtins.any (pkgs.lib.hasPrefix "https://download.mersenne.ca/") pkgs.mprime.src.urls;
  overlay = _final: prev: {
    mprime = prev.mprime.overrideAttrs (old: {
      src = old.src.overrideAttrs (src: {
        urls = map (builtins.replaceStrings
          [ "https://download.mersenne.ca/gimps/" ]
          [
            "https://www.mersenne.org/download/software/"
          ]
        ) src.urls;
      });
    });
  };
}
