# Voice Checkout design notes

The research behind the shopper's page and the decisions it led to. The page lives in `src/main.jsx` and `src/style.css`.
It replaced the first version on Oct 11, 2026.

The family view, `caregiver.html` with `src/caregiver.jsx`, uses the same colours. It lists every order in AG Grid with its risk level and scam signals. Held orders sit above the grid as cards to approve or reject.

## What changed and why

| Research finding | Applied as |
|---|---|
| Voice UIs need clear state feedback, and audio may be the only interface a user notices. Short tones ("earcons") mark listening start and end. | Two-tone beep when the mic opens and closes. The button shows state by word, icon and colour, never colour alone. A slow pulse plays only when the user has not asked for reduced motion. |
| Show what the system heard. | "You said" shows live words while listening, then the final words. |
| User control: the user must be able to stop the system. | New "Stop reading" button while speech plays. Pressing talk also interrupts speech. |
| Never a dead end; re-prompts should vary their wording and give a next step. | Each mic error has its own message and tells the user what to do. Silence, a blocked mic and a missing mic are handled separately. |
| Recognition over recall, and older users find documentation a burden. | "You can say" shows four example phrases. Each one also works as a button. |
| Keep context visible without crowding. | The latest exchange is large. Earlier turns sit in a collapsed "Earlier in this conversation" section. |
| Older adults: text at least 16px and larger is better. Let users resize text. | Base text is 150%, which is 24px by default. It is a percentage, not px, so the browser's text-size setting still applies. The old `28px` ignored it. |
| Aim for 7:1 contrast (WCAG AAA). Blue can look faded to older eyes. | Every text pair is 7:1 or higher. The accent blue was darkened. See the table below. |
| Touch targets: 44px minimum. Bigger helps older users. | Main buttons are about 53px tall. The smaller suggestion and footer buttons are about 45px. The talk button is about 130px tall. |
| Use a typeface built for legibility. | Atkinson Hyperlegible Next, made by the Braille Institute for low vision. I/l/1 and O/0 never look alike, which matters for prices. Falls back to system-ui. |
| Checkout: the full total must always be visible. Cart changes need obvious feedback. | The order panel is sticky beside the conversation on wide screens. On phones the header shows the item count and total. Lines the last reply added or changed get a highlight and an "Updated" label. |
| Scam UX: put reassurance at the point of risk, in plain words. Show security indicators consistently. | The order panel says nothing is paid until the shopper types her PIN. It shows whether PayPal is ready to pay. |
| Separate caregiver setup from the shopper's main flow. | Connect PayPal lives on the family page, behind the family key, with a message if setup fails. The shopper's footer only points family members there. |
| Spoken PINs can be overheard, and WCAG 2.2 criterion 3.3.8 discourages making people recall and transcribe codes by voice. | An on-screen PIN pad with big keys, laid out like a phone, with Clear and Delete as words rather than symbols. The PIN goes straight to `/api/pay`, never through the model, and five wrong tries pause paying for 15 minutes. |
| Use specific action labels such as "Confirm $400 transfer", not "Continue". Shoppers mistake a review step for a confirmation. | The pad's title and button both name the amount, "Pay $13.77", with the order beside it. "Not now" backs out with nothing paid. The result, paid or paused, is said and shown as its own reply. |
| Put a pause plus a named person at the point of risk, in calm words, rather than a generic warning. | A medium or high-risk payment is held before any money moves. The shopper hears one kind sentence on why, and that `TRUSTED_CONTACT` will check it. The order panel shows it is waiting, with a Call button when `TRUSTED_CONTACT_PHONE` is set, and the page says the family's answer out loud when it comes. The family gets a phone alert through ntfy, so they don't need the page open. |
| Older listeners benefit from slower speech, and needs differ. | "Reading speed" in the footer, Normal or Slower, remembered per browser. |
| Some readers with cataracts or glare prefer light text on dark. | A dark theme follows the system setting, with the same 7:1 rule. |
| A refresh should not lose the shopper's place. | The session id is kept for the tab. After a reload the cart, an open PIN pad and a held order's status all come back. |
| Screen reader users hear the app's speech on top of their reader. | "Read replies aloud" switch, remembered per browser. The reply is an `aria-live` region, so screen readers announce it when the switch is off. |
| Don't offer a control that can't work. | Browsers without speech recognition get a plain note and the type box, instead of a disabled button. |
| Spoken PINs should not be shown on screen. | If she says digits anyway, runs of 4 or more in her own words show as `••••`. The model is told to ask her to type the PIN instead. |

### Contrast checked

| Pair | Ratio |
|---|---|
| White on talk blue `#0a4f96` | 8.17 |
| White on listening red `#a3201a` | 7.56 |
| White on thinking grey `#3a3a3a` | 11.37 |
| Muted `#4a4a4a` on paper | 8.71 |
| Green `#14532d` on highlight `#eef7f0` | 8.33 |
| Dark theme: text `#f2f0ea` on card `#1f1f1f` | 14.46 |
| Dark theme: muted `#c2bfb8` on card | 8.98 |
| Dark theme: green `#9be3b0` on highlight `#12301d` | 9.55 |
| Dark theme: focus ring `#8ab8ff` on page, 3:1 needed | 9.11 |

## Still open: needs a person

These can't be automated here. Each takes a few minutes.

1. **Voice and earcons.** In desktop Chrome, allow the microphone and press the talk button. You should hear a rising tone, see your words appear, and hear a falling tone when it stops. Say "add a dozen eggs", then try Slower reading speed.
2. **Screen reader.** Turn on VoiceOver with Cmd+F5 and untick "Read replies aloud". Each reply should be announced. When the PIN pad opens, focus should land on "Your PIN", and the keys should read as numbers, "Clear", "Delete" and "Pay $X".
3. **A real approved payment.** On the family page, press Connect PayPal and sign in with the sandbox Personal account. Then buy a gift card on the shopper page, approve it on the family page, check the sandbox Business account received it, and refund it from the family view.

## Checked

- **First version, faked replies:** before it replaced the old page, the layout was checked in Chrome with `/api/chat` faked inside the tab. That covered desktop and 390px phone width, suggestions, typed turns, PIN masking, the "Updated" highlight and the listening state.
- **Oct 11, real model, scratch database:** a gift-card request after a "bank" call ran in Chrome. The model warned gently, read the order back, and opened the PIN pad instead of asking for the PIN. Keypad clicks held the order as high risk, and the PIN was not in the database. The family page showed the card. Rejecting it reached the shopper's page within seconds, and the family's key survived a reload without the link's `#key`.
- **Dark theme:** viewed with its colours applied by hand, since the test browser could not switch the system setting.
- **Oct 11, automated accessibility check:** axe-core 4.14 ran against WCAG 2.2 AA and best practice. It found no violations on the shopper page, with the PIN pad and a held order showing, and none on the family page, with a held card and the grid. Its one "needs review" item, the delete key shown as a symbol, became the word "Delete".
- **Not checked:** the three items under "Still open" above.

## Sources

- [Smashing Magazine: Can good UX protect older users from digital scams?](https://smashingmagazine.com/2025/06/can-good-ux-protect-older-users-digital-scams/)
- [Baymard: Payment UX standards](https://baymard.com/learn/payment-ux)
- [Amazon Pay: checkout best practices, citing Baymard's review-step finding](https://pay.amazon.com/blog/Best-practices-to-optimize-your-Amazon-Pay-checkout)
- [Conversational UI design patterns](https://cdn.jsdelivr.net/npm/@hegemonart/get-design-done@1.60.4/reference/conversational-ui.md)
- [uxdesign.cc: Designing for older audiences](https://uxdesign.cc/designing-for-older-audiences-checklist-best-practices-b6ca3ec5bcbf)
- [UPM Ageing Lab: UX and usability for older users](https://ageinglab.ctb.upm.es/en/ux-and-usability-for-older-users-en)
- [OzCHI 2024: Design for older people](https://outbox.eait.uq.edu.au/uqsville/OzCHI2024/Long%20Papers/Design%20for%20Older%20People%20-%20Improving%20the%20Usability%20of%20Mobile%20Apps%20through%20Targeted%20Design%20Recommendations.pdf). It notes that only 15% of published older-adult guidelines were tested with users, so treat the sizes above as good defaults, not proven thresholds.
- [Deque: WCAG 2.2 updates](https://dequeuniversity.com/resources/wcag-2.2)
