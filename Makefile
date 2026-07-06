.PHONY: init plan infra ping k8s check test cluster destroy clean

init:
	cd terraform && terraform init

plan:
	cd terraform && terraform plan

infra:
	cd terraform && terraform apply -parallelism=4

ping:
	cd ansible && ansible -i inventory.ini all -m ping

k8s:
	cd ansible && ansible-playbook -i inventory.ini site.yml

check:
	KUBECONFIG=$(PWD)/kubeconfig ./scripts/check-cluster.sh

test:
	KUBECONFIG=$(PWD)/kubeconfig kubectl apply -f kube/test/nginx.yaml
	KUBECONFIG=$(PWD)/kubeconfig kubectl get pods -o wide
	KUBECONFIG=$(PWD)/kubeconfig kubectl get svc

wait:
	./scripts/wait-for-ssh.sh

preflight:
	./scripts/preflight.sh

clean-ssh:
	for ip in 10.200.50.145 10.200.50.146 10.200.50.151 10.200.50.152 10.200.50.153 10.200.50.161 10.200.50.162; do \
		ssh-keygen -f "$$HOME/.ssh/known_hosts" -R "$$ip"; \
	done

cluster: preflight infra wait clean-ssh ping k8s check

lab: cluster ingress

destroy:
	cd terraform && terraform destroy

clean:
	rm -f kubeconfig

ingress:
	helm repo add traefik https://traefik.github.io/charts || true
	helm repo update
	KUBECONFIG=$(PWD)/kubeconfig helm upgrade --install traefik traefik/traefik \
		--namespace traefik \
		--create-namespace \
		-f kube/addons/traefik/values.yaml
	cd ansible && ansible-playbook -i inventory.ini playbooks/02-loadbalancer.yml

demo-ingress:
	KUBECONFIG=$(PWD)/kubeconfig kubectl apply -f kube/demo/nginx-ingress/namespace.yaml
	KUBECONFIG=$(PWD)/kubeconfig kubectl wait --for=jsonpath='{.status.phase}'=Active namespace/demo --timeout=30s
	KUBECONFIG=$(PWD)/kubeconfig kubectl apply -f kube/demo/nginx-ingress/service.yaml
	KUBECONFIG=$(PWD)/kubeconfig kubectl apply -f kube/demo/nginx-ingress/deployment.yaml
	KUBECONFIG=$(PWD)/kubeconfig kubectl apply -f kube/demo/nginx-ingress/ingress.yaml

delete-demo-ingress:
	KUBECONFIG=$(PWD)/kubeconfig kubectl delete -f kube/demo/nginx-ingress/ingress.yaml --ignore-not-found
	KUBECONFIG=$(PWD)/kubeconfig kubectl delete -f kube/demo/nginx-ingress/deployment.yaml --ignore-not-found
	KUBECONFIG=$(PWD)/kubeconfig kubectl delete -f kube/demo/nginx-ingress/service.yaml --ignore-not-found
	KUBECONFIG=$(PWD)/kubeconfig kubectl delete -f kube/demo/nginx-ingress/namespace.yaml --ignore-not-found

tools:
	./scripts/install-local-tools.sh
