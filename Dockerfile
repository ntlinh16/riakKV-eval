FROM debian:10

RUN apt-get update -y
RUN apt-get install -y libssl-dev logrotate sudo

COPY riak_2.2.6-1_amd64.deb ./
RUN dpkg -i riak_2.2.6-1_amd64.deb
RUN apt-get -f install -y


RUN sed -i "s|listener.http.internal = 127.0.0.1:8098|listener.http.internal = 0.0.0.0:8098|" /etc/riak/riak.conf
RUN sed -i "s|listener.protobuf.internal = 127.0.0.1:8087|listener.protobuf.internal = 0.0.0.0:8087|" /etc/riak/riak.conf

EXPOSE 8087 8098

CMD sed -i "s|nodename = riak@127.0.0.1|nodename = riak@$MY_POD_IP|" /etc/riak/riak.conf \
    && riak start && tail -f /dev/null