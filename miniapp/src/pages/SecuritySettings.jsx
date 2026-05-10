import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { ChevronLeft, Plus, Trash2, Shield, Smartphone, Monitor, Copy } from "lucide-react";
import { apiFetch } from "../utils/api";

const S = {
  bg: "var(--bg)",
  accent: "var(--accent)",
  accentDark: "var(--accent-dark)",
  text: "var(--text)",
  textMuted: "var(--text-muted)",
};

export default function SecuritySettings() {
  const navigate = useNavigate();
  const [whitelist, setWhitelist] = useState([]);
  const [currentIp, setCurrentIp] = useState("");
  const [newIp, setNewIp] = useState("");
  const [totpConfigured, setTotpConfigured] = useState(false);
  const [totpQr, setTotpQr] = useState(null);
  const [totpSecret, setTotpSecret] = useState(null);
  const [devices, setDevices] = useState([]);
  const [newDeviceName, setNewDeviceName] = useState("");
  const [newDeviceToken, setNewDeviceToken] = useState(null);
  const [toast, setToast] = useState(null);
  const [loading, setLoading] = useState(true);

  const showToast = (msg) => {
    setToast(msg);
    setTimeout(() => setToast(null), 2000);
  };

  useEffect(() => {
    Promise.all([
      apiFetch("/api/auth/whitelist").then((d) => {
        setWhitelist(d.ips || []);
        setCurrentIp(d.current_ip || "");
      }).catch(() => {}),
      apiFetch("/api/auth/totp-status").then((d) => {
        setTotpConfigured(d.configured);
      }).catch(() => {}),
      apiFetch("/api/auth/devices").then((d) => {
        setDevices(d.devices || []);
      }).catch(() => {}),
    ]).finally(() => setLoading(false));
  }, []);

  const addIp = async (ip) => {
    const target = ip || newIp.trim();
    if (!target) return;
    try {
      const d = await apiFetch("/api/auth/whitelist", {
        method: "POST",
        body: { ip: target },
      });
      setWhitelist(d.ips || []);
      setNewIp("");
      showToast("已添加");
    } catch {
      showToast("添加失败");
    }
  };

  const removeIp = async (ip) => {
    try {
      const d = await apiFetch("/api/auth/whitelist", {
        method: "DELETE",
        body: { ip },
      });
      setWhitelist(d.ips || []);
      showToast("已移除");
    } catch {
      showToast("移除失败");
    }
  };

  const setupTotp = async () => {
    try {
      const d = await apiFetch("/api/auth/totp-setup", { method: "POST" });
      setTotpQr(d.qr_code);
      setTotpSecret(d.secret);
      setTotpConfigured(true);
    } catch (e) {
      showToast(e.message || "设置失败");
    }
  };

  const deleteTotp = async () => {
    try {
      await apiFetch("/api/auth/totp", { method: "DELETE" });
      setTotpConfigured(false);
      setTotpQr(null);
      setTotpSecret(null);
      showToast("已关闭");
    } catch {
      showToast("操作失败");
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
        <h1 className="text-[17px] font-bold" style={{ color: S.text }}>
          安全设置
        </h1>
        <div className="w-10" />
      </div>

      <div className="flex-1 overflow-y-auto px-5 pb-10 pt-5 space-y-4">
        {loading ? (
          <div className="flex items-center justify-center py-20">
            <div className="h-6 w-6 rounded-full border-2 border-t-transparent animate-spin" style={{ borderColor: "var(--accent)", borderTopColor: "transparent" }} />
          </div>
        ) : <>
        {/* IP Whitelist */}
        <div
          className="rounded-[20px] p-4"
          style={{ background: S.bg, boxShadow: "var(--card-shadow)" }}
        >
          <div className="flex items-center gap-3 mb-4">
            <div
              className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full"
              style={{ boxShadow: "var(--icon-inset)", background: S.bg }}
            >
              <Shield size={18} style={{ color: S.text }} />
            </div>
            <div>
              <div className="text-[15px] font-semibold" style={{ color: S.text }}>
                IP 白名单
              </div>
              <div className="text-[11px]" style={{ color: S.textMuted }}>
                白名单内的 IP 登录后不过期
              </div>
            </div>
          </div>

          {/* Current IP */}
          <div
            className="mb-3 rounded-[12px] px-3 py-2 text-[12px]"
            style={{ background: "rgba(136,136,160,0.06)", color: S.textMuted }}
          >
            当前 IP：
            <span style={{ color: S.text, fontWeight: 600 }}>{currentIp}</span>
            {currentIp && !whitelist.includes(currentIp) && (
              <button
                className="ml-2 text-[11px] font-bold"
                style={{ color: S.accent }}
                onClick={() => addIp(currentIp)}
              >
                加入白名单
              </button>
            )}
          </div>

          {/* Whitelist items */}
          {whitelist.map((ip) => (
            <div
              key={ip}
              className="flex items-center justify-between px-3 py-2 mb-1 rounded-[10px]"
              style={{ background: "rgba(136,136,160,0.04)" }}
            >
              <span className="text-[13px] font-medium" style={{ color: S.text }}>
                {ip}
                {ip === currentIp && (
                  <span className="ml-2 text-[10px]" style={{ color: S.accent }}>当前</span>
                )}
              </span>
              <button onClick={() => removeIp(ip)}>
                <Trash2 size={14} style={{ color: S.textMuted }} />
              </button>
            </div>
          ))}

          {/* Add new IP */}
          <div className="flex gap-2 mt-3">
            <input
              type="text"
              placeholder="输入 IP 地址"
              value={newIp}
              onChange={(e) => setNewIp(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && addIp()}
              className="flex-1 rounded-[10px] px-3 py-2 text-[13px] outline-none"
              style={{ boxShadow: "var(--inset-shadow)", background: S.bg, color: S.text }}
            />
            <button
              className="flex h-9 w-9 items-center justify-center rounded-full"
              style={{
                background: "linear-gradient(135deg, var(--accent), var(--accent-dark))",
              }}
              onClick={() => addIp()}
            >
              <Plus size={16} color="white" />
            </button>
          </div>
        </div>

        {/* TOTP */}
        <div
          className="rounded-[20px] p-4"
          style={{ background: S.bg, boxShadow: "var(--card-shadow)" }}
        >
          <div className="flex items-center gap-3 mb-4">
            <div
              className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full"
              style={{ boxShadow: "var(--icon-inset)", background: S.bg }}
            >
              <Smartphone size={18} style={{ color: S.text }} />
            </div>
            <div>
              <div className="text-[15px] font-semibold" style={{ color: S.text }}>
                两步验证 (TOTP)
              </div>
              <div className="text-[11px]" style={{ color: S.textMuted }}>
                非白名单 IP 登录时需要验证码
              </div>
            </div>
          </div>

          {totpConfigured && !totpQr ? (
            <div>
              <div
                className="mb-3 rounded-[12px] px-3 py-2 text-[12px]"
                style={{ background: "rgba(136,136,160,0.06)", color: S.textMuted }}
              >
                已启用
              </div>
              <button
                className="w-full rounded-[14px] py-3 text-[14px] font-bold"
                style={{ color: S.accent, background: "rgba(201,98,138,0.08)" }}
                onClick={deleteTotp}
              >
                关闭两步验证
              </button>
            </div>
          ) : totpQr ? (
            <div className="text-center">
              <p className="text-[13px] mb-3" style={{ color: S.text }}>
                用 Authenticator App 扫描二维码
              </p>
              <img
                src={totpQr}
                alt="TOTP QR"
                className="mx-auto mb-3 rounded-[12px]"
                style={{ width: 200, height: 200 }}
              />
              <div
                className="mb-3 rounded-[12px] px-3 py-2 text-[11px] font-mono break-all"
                style={{ background: "rgba(136,136,160,0.06)", color: S.textMuted }}
              >
                密钥：{totpSecret}
              </div>
              <button
                className="w-full rounded-[14px] py-3 text-[14px] font-bold text-white"
                style={{
                  background: "linear-gradient(135deg, var(--accent), var(--accent-dark))",
                }}
                onClick={() => { setTotpQr(null); showToast("设置完成"); }}
              >
                完成
              </button>
            </div>
          ) : (
            <button
              className="w-full rounded-[14px] py-3 text-[14px] font-bold text-white"
              style={{
                background: "linear-gradient(135deg, var(--accent), var(--accent-dark))",
                boxShadow: "3px 3px 8px rgba(201,98,138,0.3)",
              }}
              onClick={setupTotp}
            >
              启用两步验证
            </button>
          )}
        </div>

        {/* Device Tokens */}
        <div
          className="rounded-[20px] p-4"
          style={{ background: S.bg, boxShadow: "var(--card-shadow)" }}
        >
          <div className="flex items-center gap-3 mb-4">
            <div
              className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full"
              style={{ boxShadow: "var(--icon-inset)", background: S.bg }}
            >
              <Monitor size={18} style={{ color: S.text }} />
            </div>
            <div>
              <div className="text-[15px] font-semibold" style={{ color: S.text }}>
                设备管理
              </div>
              <div className="text-[11px]" style={{ color: S.textMuted }}>
                授权设备连接终端，无需密码和两步验证
              </div>
            </div>
          </div>

          {/* Device list */}
          {devices.map((d) => (
            <div
              key={d.token}
              className="flex items-center justify-between px-3 py-2 mb-1 rounded-[10px]"
              style={{ background: "rgba(136,136,160,0.04)" }}
            >
              <div>
                <span className="text-[13px] font-medium" style={{ color: S.text }}>{d.name}</span>
                <div className="text-[10px]" style={{ color: S.textMuted }}>
                  {d.last_used ? `最后使用 ${d.last_used}` : "未使用"}
                </div>
              </div>
              <div className="flex items-center gap-2">
                <button onClick={async () => {
                  if (!confirm(`确定要移除设备「${d.name}」吗？移除后该设备将无法连接。`)) return;
                  try {
                    await apiFetch("/api/auth/devices", { method: "DELETE", body: { token: d.token } });
                    setDevices((prev) => prev.filter((x) => x.token !== d.token));
                    showToast("已移除");
                  } catch { showToast("移除失败"); }
                }}>
                  <Trash2 size={14} style={{ color: S.textMuted }} />
                </button>
              </div>
            </div>
          ))}

          {/* New device token display */}
          {newDeviceToken && (
            <div
              className="mb-3 rounded-[12px] px-3 py-2 text-[11px] font-mono break-all"
              style={{ background: "rgba(136,136,160,0.06)", color: S.text }}
            >
              <div className="text-[10px] mb-1" style={{ color: S.accent }}>新设备 token（仅显示一次）：</div>
              {newDeviceToken}
              <button
                className="ml-2 text-[10px] font-bold"
                style={{ color: S.accent }}
                onClick={() => { navigator.clipboard?.writeText(newDeviceToken); showToast("已复制"); }}
              >
                复制
              </button>
            </div>
          )}

          {/* Add new device */}
          <div className="flex gap-2 mt-3">
            <input
              type="text"
              placeholder="设备名称"
              value={newDeviceName}
              onChange={(e) => setNewDeviceName(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && newDeviceName.trim() && (async () => {
                try {
                  const d = await apiFetch("/api/auth/devices", { method: "POST", body: { name: newDeviceName.trim() } });
                  setNewDeviceToken(d.token);
                  setNewDeviceName("");
                  const list = await apiFetch("/api/auth/devices");
                  setDevices(list.devices || []);
                } catch { showToast("创建失败"); }
              })()}
              className="flex-1 rounded-[10px] px-3 py-2 text-[13px] outline-none"
              style={{ boxShadow: "var(--inset-shadow)", background: S.bg, color: S.text }}
            />
            <button
              className="flex h-9 w-9 items-center justify-center rounded-full"
              style={{
                background: "linear-gradient(135deg, var(--accent), var(--accent-dark))",
              }}
              onClick={async () => {
                if (!newDeviceName.trim()) return;
                try {
                  const d = await apiFetch("/api/auth/devices", { method: "POST", body: { name: newDeviceName.trim() } });
                  setNewDeviceToken(d.token);
                  setNewDeviceName("");
                  const list = await apiFetch("/api/auth/devices");
                  setDevices(list.devices || []);
                } catch { showToast("创建失败"); }
              }}
            >
              <Plus size={16} color="white" />
            </button>
          </div>
        </div>
        </>}
      </div>

      {toast && (
        <div className="pointer-events-none fixed inset-x-0 top-1/2 z-[200] flex justify-center">
          <div
            className="rounded-2xl px-6 py-3 text-[14px] font-medium text-white"
            style={{ background: "rgba(0,0,0,0.75)" }}
          >
            {toast}
          </div>
        </div>
      )}
    </div>
  );
}
