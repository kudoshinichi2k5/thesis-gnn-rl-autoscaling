{{- define "onlineboutique.podAntiAffinity" -}}
{{- $policy := .policy -}}
{{- if $policy.enabled }}
affinity:
  podAntiAffinity:
    preferredDuringSchedulingIgnoredDuringExecution:
    - weight: {{ $policy.weight }}
      podAffinityTerm:
        topologyKey: {{ $policy.topologyKey | quote }}
        labelSelector:
          matchLabels:
            app: {{ .app | quote }}
{{- end }}
{{- end -}}
