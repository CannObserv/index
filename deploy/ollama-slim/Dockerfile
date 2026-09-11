# CPU-only Ollama: 9.19GB -> 221MB (97.6%).
#
# The stock ollama/ollama image carries MLX and CUDA runner trees this cohort
# never executes — no VM in it has a GPU. Measured on the archiver VM
# (CannObserv/replicator#88): after retag and `docker system prune -f`, disk
# 98% -> 52%, 9.3G reclaimed.
#
# Deleting the directories in a derived layer does nothing: Docker layers are
# additive and the parent layers still ship. The prune has to happen in a
# throwaway stage, and the payload is copied into a clean base.
FROM ollama/ollama:latest AS pruned
RUN rm -rf /usr/lib/ollama/mlx_cuda_v13 \
           /usr/lib/ollama/cuda_v12 \
           /usr/lib/ollama/cuda_v13 \
           /usr/lib/ollama/vulkan

FROM ubuntu:24.04
# `ldd` on the binary and the CPU runners needs only these three — nothing from
# the CUDA tree.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libstdc++6 libgomp1 ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY --from=pruned /usr/bin/ollama /usr/bin/ollama
COPY --from=pruned /usr/lib/ollama /usr/lib/ollama
ENV OLLAMA_HOST=0.0.0.0:11434
EXPOSE 11434
ENTRYPOINT ["/bin/ollama"]
CMD ["serve"]
