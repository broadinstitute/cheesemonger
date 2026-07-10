def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    # Tests run without a Taiga mapping configured.
    assert body["gene_mapping"] == {"loaded": False, "entries": 0, "taiga_id": ""}


def test_health_reports_loaded_gene_mapping(client, app):
    from cheesemonger.services.gene_mappings import GeneMappingService

    app.state.gene_mapping_service = GeneMappingService(
        taiga_id="hgnc-gene-table-e250.4/hgnc_complete_set",
        entries={"1": "A1BG", "7157": "TP53"},
    )
    body = client.get("/health").json()
    assert body["gene_mapping"] == {
        "loaded": True,
        "entries": 2,
        "taiga_id": "hgnc-gene-table-e250.4/hgnc_complete_set",
    }
