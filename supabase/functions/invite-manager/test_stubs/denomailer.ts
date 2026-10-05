// CHANGE 2026-10-05 F03-FN-02: offline test stub (no previous version). Stands in for denomailer@1.6.0 in test.ts runs
// without network; the tests inject a mock sendMail instead. Same method names/argument shapes as denomailer 1.6.0.
export interface SendConfig {
  from: string;
  to: string;
  subject: string;
  content?: string;
  html?: string;
}
export class SMTPClient {
  constructor(_o: { connection: { hostname: string; port: number; tls: boolean; auth: { username: string; password: string } } }) {
    throw new Error("denomailer stub: not available in unit tests");
  }
  send(_c: SendConfig): Promise<void> {
    return Promise.resolve();
  }
  close(): Promise<void> {
    return Promise.resolve();
  }
}
