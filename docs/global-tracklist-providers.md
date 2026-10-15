# Global tracklist providers

Open Settings (the Config pane) and find **Tracklist providers**. The inventory includes disabled providers and links to the JSON provider list (`/api/remote-lookups/providers`). Rendering Settings does not contact any provider.

Enable each provider deliberately. Configure required credentials through deployment secrets and restart API/controller processes before enabling credential-dependent providers. A provider enabled without its credential remains unavailable. Credentials are never displayed by Settings or the JSON list.

Minimum intervals and requests per discovery can be changed live. A smaller interval permits faster requests; a larger budget permits more reads in one discovery. Each provider's minimum interval and maximum budget constrain these controls. Changes apply across API and controller workers before their next outbound request; requests already sent finish. Saving policy preserves all charged usage, reservations, Retry-After times and stop state. An invalid change leaves the previous policy active.

In the **Tracklist** tab, choose **Queue tracklist lookup** to search the complete active, searchable recording inventory without a reviewed tracklist selection against all enabled remote providers. Display pagination does not constrain the batch. Missing recordings, unsupported media and unusable hints are excluded. There is no minimum recording duration. Enabled providers without credentials or with a persistent stop appear as blocked work rather than issuing traffic.

Refresh batch status to see expansion, new pairs, existing in-flight pairs, exclusions and queued/running/deferred/blocked/completed/failed counts. The item list shows at most 50 pairs per batch; counts cover every pair. Deferred work resumes after its not-before time when the controller recovery pass delivers it. Recovery runs once a minute and also after expansion. Queue delivery failures leave intent saved for recovery. Restarting a worker preserves progress and successful search pages.

A blocked provider stays blocked until its policy permits requests or its persistent stop is explicitly cleared. Review the stop in Settings and provide a reason before clearing it. Clearing a stop preserves Retry-After and pacing; changing enablement or limits never clears a stop. Exhausted per-discovery budgets remain charged: raising the budget within the provider profile can permit continuation. Metadata changed during continuation is reported explicitly; queue a fresh batch for the new hints.

Open recording candidates to load a candidate, inspect its evidence and explicitly select or reject a source. Discovery and loading never automatically choose one. Existing reviewed selections remain intact; rejected provider objects stay rejected until explicitly reopened. **Link tracks to Discogs** remains a separate action over stored tracklists.
