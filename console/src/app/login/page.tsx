import { signIn } from "@/auth";

export const dynamic = "force-dynamic";

export default function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string; callbackUrl?: string }>;
}) {
  return (
    <div className="flex h-screen w-screen items-center justify-center" style={{ background: "var(--bg)" }}>
      <form
        action={async (formData) => {
          "use server";
          const params = await searchParams;
          await signIn("credentials", {
            password: formData.get("password"),
            redirectTo: params.callbackUrl || "/",
          });
        }}
        className="hairline w-[320px] p-6"
        style={{ background: "var(--panel)" }}
      >
        <div className="flex items-center gap-2 mb-5">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src="/logo-icon.svg" alt="Packet Void Labs" width={20} height={20} />
          <div className="text-[13px] font-medium" style={{ color: "var(--ink)" }}>STE</div>
          <div className="eyebrow ml-auto">CONSOLE</div>
        </div>
        <label className="eyebrow block mb-2">Operator password</label>
        <input
          name="password"
          type="password"
          autoFocus
          autoComplete="current-password"
          className="hairline mono w-full px-3 py-2 text-[13px] outline-none"
          style={{ background: "var(--bg-2)", color: "var(--ink)" }}
        />
        <button
          type="submit"
          className="hairline mono mt-4 w-full px-3 py-2 text-[12.5px]"
          style={{ background: "var(--accent)", color: "var(--bg)" }}
        >
          UNLOCK
        </button>
      </form>
    </div>
  );
}
