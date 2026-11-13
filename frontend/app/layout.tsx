import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Interaction Ledger",
  description: "Lead and call reporting for outbound voice campaigns",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
