# Start from the latest release LSST stack image.
FROM lsstsqre/centos:7-stack-lsst_distrib-w_latest

# Information about image.
ARG BUILD_DATE
LABEL lsst-desc.imsim.maintainer="https://github.com/LSSTDESC/imSim"
LABEL lsst-desc.imsim.description="A Docker image combining the LSST Science Pipelines software stack and imSim (and its dependencies)."
LABEL lsst-desc.imsim.version="latest"
LABEL lsst-desc.imsim.build_date=$BUILD_DATE

# Clone imSim and rubin_sim repos.
RUN git clone https://github.com/LSSTDESC/imSim.git &&\
    git clone https://github.com/lsst/rubin_sim.git

# Install imSim, rubin_sim, and dependencies.
RUN source /opt/lsst/software/stack/loadLSST.bash &&\
    setup lsst_distrib &&\
    pip install --upgrade galsim &&\
    pip install dust_extinction palpy batoid gitpython &&\
    pip install git+https://github.com/LSSTDESC/skyCatalogs.git@master &&\
    cd imSim && pip install -e . && cd .. &&\
    cd rubin_sim && pip install -e .

# Download Rubin Sim data.
RUN mkdir -p rubin_sim_data/sims_sed_library && \
    curl https://s3df.slac.stanford.edu/groups/rubin/static/sim-data/rubin_sim_data/skybrightness_may_2021.tgz | tar -C rubin_sim_data -xz &&\
    curl https://s3df.slac.stanford.edu/groups/rubin/static/sim-data/rubin_sim_data/throughputs_aug_2021.tgz | tar -C rubin_sim_data -xz &&\
    curl https://s3df.slac.stanford.edu/groups/rubin/static/sim-data/sed_library/seds_170124.tar.gz  | tar -C rubin_sim_data/sims_sed_library -xz

# Location of Rubin sim data (downloaded in step above).
ENV RUBIN_SIM_DATA_DIR /opt/lsst/software/stack/rubin_sim_data

# SED library (downloaded in step above).
ENV SIMS_SED_LIBRARY_DIR /opt/lsst/software/stack/rubin_sim_data/sims_sed_library