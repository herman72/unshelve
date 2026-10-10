import type { Metadata } from "next";
import { PageHeader } from "@/components/page-header";
import { SignIn } from "@/components/sign-in";

export const metadata: Metadata = { title: "Sign in" };

export default function LoginPage() {
  return (
    <>
      <PageHeader title="Sign in" description="Open the link that your Unshelve bot sends you." />
      {/* The token is in the part of the address after the #, which the server never receives. The client component reads it. */}
      <SignIn />
    </>
  );
}
