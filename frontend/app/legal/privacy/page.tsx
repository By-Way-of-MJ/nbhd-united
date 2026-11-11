import { LegalPage } from "@/components/legal-page";

export default function PrivacyPage() {
  return (
    <LegalPage title="Privacy Policy" lastUpdated="November 11, 2026">
      <h2>1. Our Commitment to Privacy</h2>
      <p>
        A personal AI assistant should know enough about you to be useful, and
        no more should leave your account. Keeping your information private is
        ongoing engineering work for us — not a marketing claim. We invest in
        our PII redaction pipeline (Section 5), access controls, and data
        minimization practices on a continuing basis, and we commit to
        continuing to do so for as long as we operate the service.
      </p>

      <h2>2. Data We Collect</h2>
      <p>We collect the following information when you use Neighborhood United:</p>
      <ul>
        <li>
          <strong>Account information</strong> — email address and display name
          provided during registration.
        </li>
        <li>
          <strong>Messaging channel identifier</strong> — if you connect a
          messaging channel, we store its account identifier to link your
          account and deliver AI assistant messages.
        </li>
        <li>
          <strong>Usage metrics</strong> — message counts and feature usage to
          monitor service health and enforce usage limits.
        </li>
        <li>
          <strong>Payment information</strong> — processed and stored by Stripe,
          or by Apple if you subscribe through the App Store in our iOS app. We
          do not store your credit card details directly.
        </li>
      </ul>

      <h2>3. How We Use Your Data</h2>
      <ul>
        <li>To provide and maintain the AI assistant service.</li>
        <li>
          To process subscription payments through Stripe or the Apple App
          Store.
        </li>
        <li>
          To communicate with you about your account, billing, and service
          updates.
        </li>
        <li>To monitor and improve service quality and reliability.</li>
      </ul>

      <h2>4. Third-Party Sharing</h2>
      <p>
        We share data with the following third parties only as necessary to
        operate the service:
      </p>
      <ul>
        <li>
          <strong>Stripe</strong> — receives your email and payment details for
          subscription billing.
        </li>
        <li>
          <strong>Messaging providers</strong> — if you connect a messaging
          channel, its provider receives the messages you send and delivers
          your assistant&apos;s responses.
        </li>
        <li>
          <strong>AI model providers</strong> (Anthropic, OpenRouter, and others)
          — receive prompts composed from your direct messages and relevant
          workspace context (notes, tasks, calendar events, tool results)
          needed to generate useful responses. Personally identifiable
          information is automatically redacted from the workspace context
          portion before sending — see Section 5 below. Each provider
          processes received data according to its own usage policies.
        </li>
        <li>
          <strong>Apple</strong> — if you subscribe through the App Store in our
          iOS app, Apple processes the payment. If you use the iOS app without a
          subscription, Apple Intelligence generates the assistant&apos;s
          replies, which can involve Apple&apos;s Private Cloud Compute — see
          Section 6 below.
        </li>
        <li>
          <strong>Microsoft Azure</strong> — hosts our infrastructure. Data is
          stored on Azure servers.
        </li>
      </ul>
      <p>
        We do not sell your personal data to advertisers or other third parties.
      </p>

      <h2>5. PII Redaction Before AI Processing</h2>
      <p>
        Before our servers send prompts to AI providers, we run an automated
        detection step that replaces personally identifiable information in the
        prompt&apos;s workspace context and tool-result portions with typed
        placeholders (e.g. <code>[PERSON_1]</code>, <code>[EMAIL_2]</code>).
        The categories we redact include names, email addresses, phone numbers,
        postal addresses, dates of birth, payment card numbers, IBAN account
        numbers, government ID numbers, IP addresses, passwords, and account,
        tax, and social security numbers.
      </p>

      <h3>What gets redacted vs. what doesn&apos;t</h3>
      <p>Each prompt sent to an AI provider is composed from two sources:</p>
      <ul>
        <li>
          <strong>Background context</strong> — your journal entries, tasks,
          calendar events, email tool results, and other workspace data the
          assistant reads to be useful. This is where most third-party PII
          lives (your contacts&apos; names and addresses, calendar attendees,
          correspondence content) and is the primary target of our redaction
          layer.
        </li>
        <li>
          <strong>Your direct messages</strong> to your assistant. These are
          sent to the provider unmodified, because the assistant needs your
          literal words to respond meaningfully — replacing names and contact
          details in your own message with placeholders degrades responses.
        </li>
      </ul>

      <h3>How detection works</h3>
      <p>
        Detection runs entirely on our servers — no third-party redaction
        service receives your data. We combine two methods: an{" "}
        <a
          href="https://huggingface.co/onbekend/nbhd-pii-model"
          target="_blank"
          rel="noopener noreferrer"
        >
          open-source machine learning model
        </a>{" "}
        (released under Apache 2.0) that reads context to distinguish PII from
        coincidental matches (e.g. &ldquo;Jordan&rdquo; the country vs. a
        person), and deterministic checksum and format validation that catches
        credit card and IBAN numbers regardless of surrounding text.
      </p>

      <h3>Limitations</h3>
      <p>
        No automated PII detector is perfect. Our model performs well on common
        PII formats but may miss unusual formats, transliterated names, niche
        identifier schemes, or PII embedded in code or structured data. The
        redaction layer is a meaningful reduction in third-party data
        exposure, not a guarantee that no PII will ever reach an AI provider.
      </p>

      <h3>Rehydration of responses</h3>
      <p>
        Because workspace context is redacted with placeholders before reaching
        the model, the model&apos;s responses come back with those same
        placeholders. To restore responses to readable form (your contacts&apos;
        real names instead of <code>[PERSON_1]</code>) before they&apos;re
        delivered to you, we maintain a per-user placeholder-to-value mapping
        in our database. This mapping is scoped to your account and is deleted
        when you delete your account.
      </p>

      <h2>6. Free Chat with Apple Intelligence</h2>
      <p>
        If you use the Neighborhood United iOS app without a subscription, the
        in-app assistant&apos;s replies are generated by Apple Intelligence
        rather than by the AI model providers our servers use.
      </p>

      <h3>When Private Cloud Compute is used</h3>
      <p>
        In Automatic mode, on supported devices, replies can be generated on
        Apple&apos;s Private Cloud Compute. With every such reply, the following
        is sent to Private Cloud Compute:
      </p>
      <ul>
        <li>
          <strong>Your message</strong> and the recent conversation.
        </li>
        <li>
          <strong>A summary of your account</strong> — your goals, tasks, recent
          journal entries, finances, workouts, a summary of your earlier chats
          with your assistant, and recent reminders it sent you.
        </li>
      </ul>
      <p>
        Anything the assistant looks up to answer is sent as well. This can
        include your journal entries, documents, tasks, goals, insights, finance
        accounts and summaries, workouts, calendar events on your device, place
        searches, and a location you choose to share.
      </p>
      <p>
        Workouts include workouts you chose to sync from Apple Health into
        Neighborhood United. We do not otherwise read Apple Health data for
        these replies.
      </p>
      <p>
        The PII redaction in Section 5 is applied by our servers to prompts they
        send to AI model providers. It is not applied to free chat, so this
        information is sent to Apple as it appears in your account.
      </p>
      <p>
        Apple states that data sent to Private Cloud Compute is used only to
        fulfill your request, is not retained after the response is returned,
        and is not accessible to Apple. See{" "}
        <a
          href="https://security.apple.com/blog/private-cloud-compute/"
          target="_blank"
          rel="noopener noreferrer"
        >
          Apple&apos;s description of Private Cloud Compute
        </a>
        .
      </p>

      <h3>When nothing is sent to Apple</h3>
      <p>
        In Private mode, or when Private Cloud Compute is unavailable, the reply
        is generated by Apple Intelligence on your device, and nothing is sent
        to Apple to generate it. Siri requests on the free tier are answered on
        your device only.
      </p>

      <h3>Free chats are saved to your account</h3>
      <p>
        Your free chats (your messages and the replies) are saved to your
        Neighborhood United account, like any other chat, so they are still
        there if you subscribe.
      </p>

      <h2>7. Data Retention</h2>
      <p>
        We retain your account data for as long as you keep your account. If
        you cancel your subscription and request account deletion, we will
        delete your personal data within 30 days, except where retention is
        required by law or for legitimate business purposes (e.g., billing
        records).
      </p>

      <h2>8. Cookies & Local Storage</h2>
      <p>
        We use browser local storage to store authentication tokens (JWT) for
        session management. We do not use tracking cookies, advertising cookies,
        or third-party analytics trackers.
      </p>

      <h2>9. Your Rights</h2>
      <p>You have the right to:</p>
      <ul>
        <li>
          <strong>Access</strong> your personal data — contact us to request a
          copy of the data we hold about you.
        </li>
        <li>
          <strong>Correct</strong> inaccurate data — update your display name
          and email through your account settings.
        </li>
        <li>
          <strong>Delete</strong> your data — request account deletion by
          contacting us at the email below.
        </li>
      </ul>

      <h2>10. Security</h2>
      <p>
        We use industry-standard security measures including encrypted
        connections (HTTPS/TLS), secure authentication tokens, and access
        controls. However, no method of transmission over the internet is 100%
        secure, and we can&apos;t guarantee absolute security.
      </p>

      <h2>11. Children&apos;s Privacy</h2>
      <p>
        Neighborhood United is not intended for use by anyone under the age of 13. We do
        not knowingly collect personal information from children under 13. If we
        become aware that we have collected data from a child under 13, we will
        take steps to delete it promptly.
      </p>

      <h2>12. Changes to This Policy</h2>
      <p>
        We may update this privacy policy from time to time. If we make material
        changes, we will notify you via email or through the service.
      </p>

      <h2>13. Contact</h2>
      <p>
        For privacy-related questions or requests, contact us at{" "}
        <a href="mailto:mj@bywayofmj.com">mj@bywayofmj.com</a>.
      </p>
    </LegalPage>
  );
}
