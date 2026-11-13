export {};

declare global {
  interface BotDesktopAppUpdateCheckResult {
    ok: boolean;
    reason?: string | null;
    currentVersion?: string;
    latestVersion?: string | null;
    hasUpdate: boolean;
  }

  interface BotDesktopAppUpdateResult {
    ok: boolean;
    reason?: string | null;
  }

  interface BotDesktopAppUpdateProgress {
    phase: "downloading" | "verifying" | "installing";
    downloadedBytes: number;
    totalBytes?: number | null;
  }

  interface BotAppUpdaterBridge {
    checkForAppUpdate: () => Promise<BotDesktopAppUpdateCheckResult>;
    installAppUpdate: (
      onProgress?: (progress: BotDesktopAppUpdateProgress) => void,
    ) => Promise<BotDesktopAppUpdateResult>;
  }

  interface Window {
    botAppUpdater?: BotAppUpdaterBridge;
    astrbotDesktop?: {
      isDesktop: boolean;
      isDesktopRuntime: () => Promise<boolean>;
      getBackendState: () => Promise<{
        running: boolean;
        spawning: boolean;
        restarting: boolean;
        canManage: boolean;
      }>;
      restartBackend: (authToken?: string | null) => Promise<{
        ok: boolean;
        reason: string | null;
      }>;
      stopBackend: () => Promise<{
        ok: boolean;
        reason: string | null;
      }>;
      pickDirectory?: (defaultPath?: string | null) => Promise<string | null>;
      setWindowTheme?: (theme: 'dark' | 'light' | null) => Promise<{
        ok: boolean;
        reason: string | null;
      }>;
      minimizeWindow?: () => Promise<{
        ok: boolean;
        reason: string | null;
      }>;
      toggleMaximizeWindow?: () => Promise<{
        ok: boolean;
        reason: string | null;
      }>;
      closeWindow?: () => Promise<{
        ok: boolean;
        reason: string | null;
      }>;
      onTrayRestartBackend?: (callback: () => void) => () => void;
    };
  }
}
