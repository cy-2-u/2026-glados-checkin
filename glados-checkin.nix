self:
{
  config,
  lib,
  pkgs,
  ...
}:

with lib;

let
  cfg = config.services.glados-checkin;
in
{
  options.services.glados-checkin = {
    enable = mkEnableOption "GLaDOS Automatic Check-in Service";

    cookie = mkOption {
      type = types.str;
      description = "GLaDOS Cookie (or multiple cookies separated by && or newline). Keep it secret.";
    };

    pushLevel = mkOption {
      type = types.enum [
        "all"
        "fail_only"
      ];
      default = "all";
      description = "Level of notification pushing: 'all' or 'fail_only'.";
    };

    exchangePlan = mkOption {
      type = types.nullOr (
        types.enum [
          "100"
          "200"
          "500"
        ]
      );
      default = null;
      description = "Auto-exchange plan: 100/200/500 points. Null disables exchange.";
    };
  };

  config = mkIf cfg.enable {
    systemd.services.glados-checkin = {
      description = "GLaDOS Automatic Check-in";
      wants = [ "network-online.target" ];
      after = [ "network-online.target" ];

      environment = {
        GLADOS_COOKIE = cfg.cookie;
        PUSH_LEVEL = cfg.pushLevel;
      }
      // (optionalAttrs (cfg.exchangePlan != null) {
        GLADOS_EXCHANGE_PLAN = cfg.exchangePlan;
      });

      serviceConfig = {
        Type = "oneshot";
        ExecStart = "${self.packages.${pkgs.system}.default}/bin/glados-checkin";
        DynamicUser = true;
      };
    };

    systemd.timers.glados-checkin = {
      description = "Timer for GLaDOS Automatic Check-in";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = [
          "*-*-* 09:30:00"
        ];
        RandomizedDelaySec = "10m";
        Persistent = true;
      };
    };
  };
}
