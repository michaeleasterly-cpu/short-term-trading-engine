import NextAuth, { CredentialsSignin } from "next-auth";
import Credentials from "next-auth/providers/credentials";

class WrongPassword extends CredentialsSignin {
  code = "wrong-password";
}

export const { handlers, signIn, signOut, auth } = NextAuth({
  trustHost: true,
  session: { strategy: "jwt" },
  pages: { signIn: "/login" },
  providers: [
    Credentials({
      name: "Operator",
      credentials: { password: { label: "Password", type: "password" } },
      authorize: async (credentials) => {
        const expected = process.env.CONSOLE_PASSWORD;
        if (!expected) {
          throw new Error("CONSOLE_PASSWORD env var not set on the deployment");
        }
        if (credentials?.password === expected) {
          return { id: "operator", name: "Operator" };
        }
        throw new WrongPassword();
      },
    }),
  ],
  callbacks: {
    authorized: async ({ auth }) => !!auth?.user,
  },
});
