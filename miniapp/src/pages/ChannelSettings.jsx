import { useState, useEffect, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { ChevronLeft, MessageCircle, Unplug, QrCode, Loader2, CheckCircle2, XCircle } from "lucide-react";
import { QRCodeSVG } from "qrcode.react";
import { apiFetch } from "../utils/api";

const S = {
  bg: "var(--bg)",
  accent: "var(--accent)",
  accentDark: "var(--accent-dark)",
  text: "var(--text)",
  textMuted: "var(--text-muted)",
};

export default function ChannelSettings() {
  const navigate = useNavigate();
  const [loading, setLoading] = useState(true);
  const [connected, setConnected] = useState(false);
  const [toast, setToast] = useState(null);

  // QR login state
  const [qrUrl, setQrUrl] = useState(null);
  const [qrcode, setQrcode] = useState(null);
  const [scanning, setScanning] = useState(false);
  const [qrStatus, setQrStatus] = useState(null); // "waiting" | "confirmed" | "expired"
  const pollRef = useRef(null);

  const showToast = (msg) => {
    setToast(msg);
    setTimeout(() => setToast(null), 2500);
  };

  // Load status on mount
  useEffect(() => {
    apiFetch("/api/wechat/status")
      .then((d) => setConnected(d.connected))
      .catch(() => {})
      .finally(() => setLoading(false));
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, []);

  const startLogin = async () => {
    setScanning(true);
    setQrStatus("waiting");
    setQrUrl(null);
    try {
      const d = await apiFetch("/api/wechat/qr-login", { method: "POST" });
      setQrUrl(d.qr_url);
      setQrcode(d.qrcode);
      // Start polling status
      pollRef.current = setInterval(async () => {
        try {
          const s = await apiFetch(`/api/wechat/qr-status?qrcode=${encodeURIComponent(d.qrcode)}`);
          setQrStatus(s.status);
          if (s.status === "confirmed") {
            clearInterval(pollRef.current);
            pollRef.current = null;
            setConnected(true);
            setScanning(false);
            showToast("连接成功");
          } else if (s.status === "expired") {
            clearInterval(pollRef.current);
            pollRef.current = null;
            setScanning(false);
            showToast("二维码已过期，请重试");
          }
        } catch {
          // ignore poll errors
        }
      }, 2000);
    } catch (e) {
      setScanning(false);
      showToast("获取二维码失败");
    }
  };

  const cancelLogin = () => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    setScanning(false);
    setQrUrl(null);
    setQrcode(null);
    setQrStatus(null);
  };

  const disconnect = async () => {
    try {
      await apiFetch("/api/wechat/disconnect", { method: "POST" });
      setConnected(false);
      showToast("已断开");
    } catch {
      showToast("断开失败");
    }
  };

  return (
    <div className="flex h-full flex-col" style={{ background: S.bg }}>
      {/* Header */}
      <div
        className="flex shrink-0 items-center justify-between px-5 pb-4"
        style={{ paddingTop: "max(1.25rem, env(safe-area-inset-top))" }}
      >
        <button
          className="flex h-10 w-10 items-center justify-center rounded-full"
          style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)" }}
          onClick={() => navigate("/settings", { replace: true })}
        >
          <ChevronLeft size={22} style={{ color: S.text }} />
        </button>
        <h1 className="text-[17px] font-bold" style={{ color: S.text }}>渠道管理</h1>
        <div className="w-10" />
      </div>

      <div className="flex-1 overflow-y-auto px-5 pb-10 pt-5 space-y-4">

        {/* WeChat card */}
        <div
          className="rounded-[20px] overflow-hidden p-5"
          style={{ background: S.bg, boxShadow: "var(--card-shadow)" }}
        >
          {/* Title row */}
          <div className="flex items-center gap-3 mb-4">
            <div
              className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full"
              style={{ boxShadow: "var(--icon-inset)", background: S.bg }}
            >
              <MessageCircle size={18} style={{ color: S.text }} />
            </div>
            <div className="flex-1">
              <div className="text-[15px] font-semibold" style={{ color: S.text }}>微信</div>
              <div className="text-[11px]" style={{ color: S.textMuted }}>iLink 智联协议</div>
            </div>
            {/* Status badge */}
            {!loading && (
              <div className="flex items-center gap-1.5">
                {connected ? (
                  <>
                    <CheckCircle2 size={14} style={{ color: "#22c55e" }} />
                    <span className="text-[12px] font-medium" style={{ color: "#22c55e" }}>已连接</span>
                  </>
                ) : (
                  <>
                    <XCircle size={14} style={{ color: S.textMuted }} />
                    <span className="text-[12px] font-medium" style={{ color: S.textMuted }}>未连接</span>
                  </>
                )}
              </div>
            )}
          </div>

          {/* Divider */}
          <div className="h-px mb-4" style={{ background: "rgba(136,136,160,0.15)" }} />

          {loading ? (
            /* Loading placeholder — same height as button row to prevent jump */
            <div className="flex items-center justify-center gap-2" style={{ height: 44 }}>
              <Loader2 size={16} className="animate-spin" style={{ color: S.accent }} />
            </div>
          ) : (
            <>
              {/* QR code area */}
              {scanning && qrUrl && (
                <div className="flex flex-col items-center gap-3 mb-4">
                  <div
                    className="rounded-[16px] p-4"
                    style={{ background: "#fff", boxShadow: "var(--inset-shadow)" }}
                  >
                    <QRCodeSVG value={qrUrl} size={192} level="M" />
                  </div>
                  <div className="flex items-center gap-2">
                    <Loader2 size={14} className="animate-spin" style={{ color: S.accent }} />
                    <span className="text-[12px]" style={{ color: S.textMuted }}>
                      请用微信扫描二维码
                    </span>
                  </div>
                  <button
                    onClick={cancelLogin}
                    className="text-[12px] font-medium px-4 py-1.5 rounded-full"
                    style={{ color: S.textMuted, boxShadow: "var(--card-shadow-sm)", background: S.bg }}
                  >
                    取消
                  </button>
                </div>
              )}

              {scanning && !qrUrl && (
                <div className="flex items-center justify-center gap-2 mb-4 py-6">
                  <Loader2 size={16} className="animate-spin" style={{ color: S.accent }} />
                  <span className="text-[13px]" style={{ color: S.textMuted }}>正在获取二维码...</span>
                </div>
              )}

              {/* Action buttons */}
              {!scanning && (
                <div className="flex gap-3">
                  {connected ? (
                    <>
                      <button
                        onClick={startLogin}
                        className="flex-1 flex items-center justify-center gap-2 py-3 rounded-[14px] text-[13px] font-semibold text-white"
                        style={{
                          background: `linear-gradient(135deg, ${S.accent} 0%, ${S.accentDark} 100%)`,
                          boxShadow: "var(--card-shadow-sm)",
                        }}
                      >
                        <QrCode size={15} />
                        重新登录
                      </button>
                      <button
                        onClick={disconnect}
                        className="flex items-center justify-center gap-2 px-5 py-3 rounded-[14px] text-[13px] font-semibold"
                        style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)", color: "#ef4444" }}
                      >
                        <Unplug size={15} />
                        断开
                      </button>
                    </>
                  ) : (
                    <button
                      onClick={startLogin}
                      className="flex-1 flex items-center justify-center gap-2 py-3 rounded-[14px] text-[13px] font-semibold text-white"
                      style={{
                        background: `linear-gradient(135deg, ${S.accent} 0%, ${S.accentDark} 100%)`,
                        boxShadow: "var(--card-shadow-sm)",
                      }}
                    >
                      <QrCode size={15} />
                      扫码登录
                    </button>
                  )}
                </div>
              )}
            </>
          )}
        </div>

        {/* Hint */}
        <div className="px-2">
          <p className="text-[11px] leading-relaxed" style={{ color: S.textMuted }}>
            通过微信 iLink 协议连接后，可以在微信中直接与助手A聊天。
            连接后支持平台间自动切换（Telegram / QQ / 微信）。
          </p>
        </div>
      </div>

      {/* Toast */}
      {toast && (
        <div
          className="fixed left-1/2 top-16 -translate-x-1/2 z-50 rounded-full px-5 py-2 text-[13px] font-medium text-white"
          style={{
            background: "linear-gradient(135deg, var(--accent) 0%, var(--accent-dark) 100%)",
            boxShadow: "var(--card-shadow)",
          }}
        >
          {toast}
        </div>
      )}
    </div>
  );
}
